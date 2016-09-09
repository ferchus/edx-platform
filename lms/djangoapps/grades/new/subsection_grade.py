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
    def __init__(self, subsection):
        self.location = subsection.location
        self.display_name = block_metadata_utils.display_name_with_default_escaped(subsection)
        self.url_name = block_metadata_utils.url_name_for_block(subsection)

        self.format = getattr(subsection, 'format', '')
        self.due = getattr(subsection, 'due', None)
        self.graded = getattr(subsection, 'graded', False)

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

    def save(self, student, subsection, course):
        """
        Persist the SubsectionGrade.
        """
        visible_blocks = [
            BlockRecord(location, weight, score.possible)
            for location, (score, weight) in self.locations_to_weighted_scores.iteritems()
        ]

        PersistentSubsectionGrade.save_grade(
            user_id=student.id,
            usage_key=self.location,
            course_version=getattr(course, 'course_version', None),
            subtree_edited_timestamp=subsection.subtree_edited_on,
            earned_all=self.all_total.earned,
            possible_all=self.all_total.possible,
            earned_graded=self.graded_total.earned,
            possible_graded=self.graded_total.possible,
            visible_blocks=visible_blocks,
        )

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

    def create(self, subsection, block_structure=None):
        """
        Returns the SubsectionGrade object for the student and subsection.

        Optionally takes in a block_structure
        """
        block_structure = self._get_block_structure(block_structure)
        return (
            self._get_saved_grade(subsection, block_structure) or
            self._compute_and_save_grade(subsection, block_structure)
        )

    def update(self, usage_key, block_structure=None):
        """
        Updates the SubsectionGrade object for the student and subsection
        identified by the given usage key.
        """
        # save ourselves the extra queries if the course does not use subsection grades
        if not PersistentGradesEnabledFlag.feature_enabled(self.course.id):
            return

        block_structure = self._get_block_structure(block_structure)
        subsection = block_structure[usage_key]
        return self._compute_and_save_grade(subsection, block_structure)

    def _compute_and_save_grade(self, subsection, block_structure):
        """
        Freshly computes and updates the grade for the student and subsection.
        """
        subsection_grade = SubsectionGrade(subsection)
        subsection_grade.compute(self.student, block_structure, self._scores_client, self._submissions_scores)
        self._save_grade(subsection_grade, subsection)
        return subsection_grade

    def _get_saved_grade(self, subsection, block_structure):  # pylint: disable=unused-argument
        """
        Returns the saved grade for the student and subsection.
        """
        if not PersistentGradesEnabledFlag.feature_enabled(self.course.id):
            return

        saved_subsection_grade =self._get_saved_subsection_grade(subsection.location)
        if saved_subsection_grade:
            subsection_grade = SubsectionGrade(subsection)
            subsection_grade.load_from_data(
                saved_subsection_grade, block_structure, self._scores_client, self._submissions_scores
            )
            return subsection_grade

    def _save_grade(self, subsection_grade, subsection):
        """
        Updates the saved grade for the student and subsection.
        """
        if not PersistentGradesEnabledFlag.feature_enabled(self.course.id):
            return
        subsection_grade.save(self.student, subsection, self.course)

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

    @lazy
    def _saved_subsection_grades(self):
        """
        Lazily queries and returns the persistent subsection
        grades for the course, while caching the result.
        """
        return PersistentSubsectionGrade.read_all_grades_for_course(self.student.id, self.course.id)

    def _get_saved_subsection_grade(self, subsection_usage_key):
        """
        Returns the saved subsection grade for the given
        subsection usage key.
        Returns None if not found.
        """
        for record in self._saved_subsection_grades:
            if record.usage_key == subsection_usage_key:
                return record

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
