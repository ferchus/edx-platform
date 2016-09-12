"""
SubsectionGrade Class
"""
from collections import OrderedDict
from lazy import lazy

from courseware.model_data import ScoresClient
from lms.djangoapps.grades.scores import get_score, possibly_scored
from lms.djangoapps.grades.models import BlockRecord, PersistentSubsectionGrade
from lms.djangoapps.grades.config.models import PersistentGradesEnabledFlag
from student.models import anonymous_id_for_user, User
from submissions import api as submissions_api
from xmodule import block_metadata_utils, graders
from xmodule.graders import Score


class SubsectionGrade(object):
    """
    Class for Subsection Grades.
    """
    def __init__(self, subsection, course):
        self.location = subsection.location
        self.display_name = block_metadata_utils.display_name_with_default_escaped(subsection)
        self.url_name = block_metadata_utils.url_name_for_block(subsection)

        self.format = getattr(subsection, 'format', '')
        self.due = getattr(subsection, 'due', None)
        self.graded = getattr(subsection, 'graded', False)

        self.course_version = getattr(course, 'course_version', None)
        self.subtree_edited_timestamp = subsection.subtree_edited_on

        self.graded_total = None  # aggregated grade for all graded problems
        self.all_total = None  # aggregated grade for all problems, regardless of whether they are graded
        self.locations_to_weighted_scores = OrderedDict()  # dict of problem locations to (Score, weight) tuples

    @lazy
    def scores(self):
        """
        List of all problem scores in the subsection.
        """
        return [score for score, _ in self.locations_to_weighted_scores.itervalues()]

    def compute(self, student, course_structure, scores_client, submissions_scores):
        """
        Compute the grade of this subsection for the given student and course.
        """
        try:
            for descendant_key in course_structure.post_order_traversal(
                    filter_func=possibly_scored,
                    start_node=self.location,
            ):
                self._compute_block_score(student, descendant_key, course_structure, scores_client, submissions_scores)
        finally:
            # self.scores may hold outdated data, force it to refresh on next access
            lazy.invalidate(self, 'scores')

        self.all_total, self.graded_total = graders.aggregate_scores(self.scores, self.display_name, self.location)

    @property
    def _get_visible_blocks(self):
        """
        Returns the list of visible blocks.
        """
        return [
            BlockRecord(location, weight, score.possible)
            for location, (score, weight) in self.locations_to_weighted_scores.iteritems()
        ]

    def _persisted_model_params(self, student):
        """
        Returns the parameters for creating/updating the
        persisted model for this subsection grade.
        """
        return dict(
            user_id=student.id,
            usage_key=self.location,
            course_version=self.course_version,
            subtree_edited_timestamp=self.subtree_edited_timestamp,
            earned_all=self.all_total.earned,
            possible_all=self.all_total.possible,
            earned_graded=self.graded_total.earned,
            possible_graded=self.graded_total.possible,
            visible_blocks=self._get_visible_blocks,
        )

    @classmethod
    def bulk_create_models(cls, student, subsection_grades):
        """
        Saves the subsection grade in a persisted model.
        """
        return PersistentSubsectionGrade.bulk_create_grades([
            subsection_grade._persisted_model_params(student)
            for subsection_grade in subsection_grades
        ])

    def create_model(self, student):
        """
        Saves the subsection grade in a persisted model.
        """
        return PersistentSubsectionGrade.create_grade(**self._persisted_model_params(student))

    def update_or_create_model(self, student):
        """
        Saves or updates the subsection grade in a persisted model.
        """
        return PersistentSubsectionGrade.update_or_create_grade(**self._persisted_model_params(student))

    def load_from_data(self, model, course_structure, scores_client, submissions_scores):
        """
        Load the subsection grade from the persisted model.
        """
        for block in model.visible_blocks.blocks:
            persisted_values = {'weight': block.weight, 'possible': block.max_score}
            self._compute_block_score(
                User.objects.get(id=model.user_id),
                block.locator,
                course_structure,
                scores_client,
                submissions_scores,
                persisted_values
            )

        self.graded_total = Score(
            earned=model.earned_graded,
            possible=model.possible_graded,
            graded=True,
            section=self.display_name,
            module_id=self.location,
        )
        self.all_total = Score(
            earned=model.earned_all,
            possible=model.possible_all,
            graded=False,
            section=self.display_name,
            module_id=self.location,
        )

    def _compute_block_score(
            self,
            student,
            block_key,
            course_structure,
            scores_client,
            submissions_scores,
            persisted_values=None,
    ):
        """
        Compute score for the given block. If persisted_values is provided, it will be used for possible and weight.
        """
        block = course_structure[block_key]

        if getattr(block, 'has_score', False):
            (earned, possible) = get_score(
                student,
                block,
                scores_client,
                submissions_scores,
            )

            # There's a chance that the value of weight is not the same value used when the problem was scored,
            # since we can get the value from either block_structure or CSM/submissions.
            weight = getattr(block, 'weight', None)
            if persisted_values:
                possible = persisted_values.get('possible', possible)
                weight = persisted_values.get('weight', weight)

            if earned is not None or possible is not None:
                # cannot grade a problem with a denominator of 0
                block_graded = block.graded if possible > 0 else False

                self.locations_to_weighted_scores[block.location] = (
                    Score(
                        earned,
                        possible,
                        block_graded,
                        block_metadata_utils.display_name_with_default_escaped(block),
                        block.location,
                    ),
                    weight,
                )


class SubsectionGradeFactory(object):
    """
    Factory for Subsection Grades.
    """
    def __init__(self, student, course, course_structure):
        self.student = student
        self.course = course
        self.course_structure = course_structure

        self._cached_subsection_grades = None

    def create(self, subsection, block_structure=None, read_only=False):
        """
        Returns the SubsectionGrade object for the student and subsection.

        If block_structure is provided, uses it for finding and computing
        the grade instead of the course_structure passed in earlier.

        If read_only is True, doesn't save any updates to the grades.
        """
        block_structure = self._get_block_structure(block_structure)

        subsection_grade = self._get_saved_grade(subsection, block_structure)
        if not subsection_grade:
            subsection_grade = SubsectionGrade(subsection, self.course)
            subsection_grade.compute(self.student, block_structure, self._scores_client, self._submissions_scores)
            if not read_only and PersistentGradesEnabledFlag.feature_enabled(self.course.id):
                grade_model = subsection_grade.create_model(self.student)
                self._update_saved_subsection_grade(subsection.location, grade_model)
        return subsection_grade

    def bulk_create(self, subsection_grades):
        """
        Bulks creates all the models for the given subsection_grades.
        """
        SubsectionGrade.bulk_create_models(self.student, subsection_grades)

    def update(self, subsection, block_structure=None):
        """
        Updates the SubsectionGrade object for the student and subsection
        identified by the given usage key.
        """
        # Save ourselves the extra queries if the course does not persist
        # subsection grades.
        if not PersistentGradesEnabledFlag.feature_enabled(self.course.id):
            return

        block_structure = self._get_block_structure(block_structure)
        subsection_grade = SubsectionGrade(subsection, self.course)
        subsection_grade.compute(self.student, block_structure, self._scores_client, self._submissions_scores)

        grade_model = subsection_grade.update_or_create_model(self.student)
        self._update_saved_subsection_grade(subsection.location, grade_model)
        return subsection_grade

    @lazy
    def _scores_client(self):
        """
        Lazily queries and returns all the scores stored in the user
        state (in CSM) for the course, while caching the result.
        """
        scorable_locations = [block_key for block_key in self.course_structure if possibly_scored(block_key)]
        return ScoresClient.create_for_locations(self.course.id, self.student.id, scorable_locations)

    @lazy
    def _submissions_scores(self):
        """
        Lazily queries and returns the scores stored by the
        Submissions API for the course, while caching the result.
        """
        anonymous_user_id = anonymous_id_for_user(self.student, self.course.id)
        return submissions_api.get_scores(unicode(self.course.id), anonymous_user_id)

    def _get_saved_grade(self, subsection, block_structure):  # pylint: disable=unused-argument
        """
        Returns the saved grade for the student and subsection.
        """
        if not PersistentGradesEnabledFlag.feature_enabled(self.course.id):
            return

        saved_subsection_grade = self._get_saved_subsection_grade(subsection.location)
        if saved_subsection_grade:
            subsection_grade = SubsectionGrade(subsection, self.course)
            subsection_grade.load_from_data(
                saved_subsection_grade, block_structure, self._scores_client, self._submissions_scores
            )
            return subsection_grade

    def _get_saved_subsection_grade(self, subsection_usage_key):
        """
        Returns the saved value of the subsection grade for
        the given subsection usage key, caching the value.
        Returns None if not found.
        """
        if self._cached_subsection_grades is None:
            self._cached_subsection_grades = {
                record.full_usage_key: record
                for record in PersistentSubsectionGrade.read_grades_for_user_in_course(self.student.id, self.course.id)
            }
        return self._cached_subsection_grades.get(subsection_usage_key)

    def _update_saved_subsection_grade(self, subsection_usage_key, subsection_model):
        """
        Updates (or adds) the subsection grade for the given
        subsection usage key in the local cache, iff the cache
        is populated.
        """
        if self._cached_subsection_grades is not None:
            self._cached_subsection_grades[subsection_usage_key] = subsection_model

    def _get_block_structure(self, block_structure):
        """
        If block_structure is None, returns self.course_structure.
        Otherwise, returns block_structure after verifying that the
        given block_structure is a sub-structure of self.course_structure.
        """
        if block_structure:
            if block_structure.root_block_usage_key not in self.course_structure:
                raise ValueError
            return block_structure
        else:
            return self.course_structure
