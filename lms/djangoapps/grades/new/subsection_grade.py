"""
SubsectionGrade Class
"""
from collections import OrderedDict
from lazy import lazy
from logging import getLogger
from courseware.model_data import ScoresClient
from lms.djangoapps.grades.scores import get_score, possibly_scored
from lms.djangoapps.grades.models import BlockRecord, PersistentSubsectionGrade
from lms.djangoapps.grades.config.models import PersistentGradesEnabledFlag
from student.models import anonymous_id_for_user, User
from submissions import api as submissions_api
from xmodule import block_metadata_utils, graders
from xmodule.graders import Score


log = getLogger(__name__)


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
        log.info(u"Persistent Grades: calculated scores property for subsection {0}".format(self.location))
        return [score for score, _ in self.locations_to_weighted_scores.itervalues()]

    def compute(self, student, course_structure, scores_client, submissions_scores):
        """
        Compute the grade of this subsection for the given student and course.
        """
        lazy.invalidate(self, 'scores')
        for descendant_key in course_structure.post_order_traversal(
                filter_func=possibly_scored,
                start_node=self.location,
        ):
            self._compute_block_score(student, descendant_key, course_structure, scores_client, submissions_scores)
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

    def __unicode__(self):
        """
        Provides a unicode representation of the scoring
        data for this subsection. Used for logging.
        """
        return u"SubsectionGrade|total: {0}/{1}|graded: {2}/{3}|location: {4}|display name: {5}".format(
            self.all_total.earned,
            self.all_total.possible,
            self.graded_total.earned,
            self.graded_total.possible,
            self.location,
            self.display_name
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
    def __init__(self, student):
        self.student = student

        self._scores_client = None
        self._submissions_scores = None

    def create(self, subsection, course_structure, course):
        """
        Returns the SubsectionGrade object for the student and subsection.
        """
        self._prefetch_scores(course_structure, course)
        return (
            self._get_saved_grade(subsection, course_structure, course) or
            self._compute_and_save_grade(subsection, course_structure, course)
        )

    def update(self, usage_key, course_structure, course):
        """
        Updates the SubsectionGrade object for the student and subsection
        identified by the given usage key.
        """
        # save ourselves the extra queries if the course does not use subsection grades
        if not PersistentGradesEnabledFlag.feature_enabled(course.id):
            return

        self._prefetch_scores(course_structure, course)
        subsection = course_structure[usage_key]
        return self._compute_and_save_grade(subsection, course_structure, course)

    def _compute_and_save_grade(self, subsection, course_structure, course):
        """
        Freshly computes and updates the grade for the student and subsection.
        """
        subsection_grade = SubsectionGrade(subsection)
        subsection_grade.compute(self.student, course_structure, self._scores_client, self._submissions_scores)
        self._save_grade(subsection_grade, subsection, course)
        return subsection_grade

    def _get_saved_grade(self, subsection, course_structure, course):  # pylint: disable=unused-argument
        """
        Returns the saved grade for the student and subsection.
        """
        if PersistentGradesEnabledFlag.feature_enabled(course.id):
            try:
                model = PersistentSubsectionGrade.read_grade(
                    user_id=self.student.id,
                    usage_key=subsection.location,
                )
                subsection_grade = SubsectionGrade(subsection)
                subsection_grade.load_from_data(model, course_structure, self._scores_client, self._submissions_scores)
                log.warning(
                    u"Persistent Grades: Loaded grade for course id: {0}, version: {1}, subtree edited on: {2},"
                    u" grade: {3}, user: {4}".format(
                        course.id,
                        getattr(course, 'course_version', None),
                        course.subtree_edited_on,
                        subsection_grade,
                        self.student.id
                    )
                )
                return subsection_grade
            except PersistentSubsectionGrade.DoesNotExist:
                log.warning(
                    u"Persistent Grades: Could not find grade for course id: {0}, version: {1}, subtree edited"
                    u" on: {2}, subsection: {3}, user: {4}".format(
                        course.id,
                        getattr(course, 'course_version', None),
                        course.subtree_edited_on,
                        subsection.location,
                        self.student.id
                    )
                )
                return None

    def _save_grade(self, subsection_grade, subsection, course):  # pylint: disable=unused-argument
        """
        Updates the saved grade for the student and subsection.
        """
        if PersistentGradesEnabledFlag.feature_enabled(course.id):
            subsection_grade.save(self.student, subsection, course)
        log.warning(u"Persistent Grades: Saved grade for course id: {0}, version: {1}, subtree_edited_on: {2}, grade: "
                    u"{3}, user: {4}"
                    .format(
                        course.id,
                        getattr(course, 'course_version', None),
                        course.subtree_edited_on,
                        subsection_grade,
                        self.student.id
                    ))

    def _prefetch_scores(self, course_structure, course):
        """
        Returns the prefetched scores for the given student and course.
        """
        if not self._scores_client:
            scorable_locations = [block_key for block_key in course_structure if possibly_scored(block_key)]
            self._scores_client = ScoresClient.create_for_locations(
                course.id, self.student.id, scorable_locations
            )
            self._submissions_scores = submissions_api.get_scores(
                unicode(course.id), anonymous_id_for_user(self.student, course.id)
            )
