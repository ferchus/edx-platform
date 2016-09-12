"""
Models used for robust grading.

Robust grading allows student scores to be saved per-subsection independent
of any changes that may occur to the course after the score is achieved.
"""

from base64 import b64encode
from collections import namedtuple
from hashlib import sha1
import json
import logging
from operator import attrgetter

from django.db import models, transaction
from django.db.utils import IntegrityError
from model_utils.models import TimeStampedModel

from coursewarehistoryextended.fields import UnsignedBigIntAutoField
from opaque_keys.edx.keys import CourseKey, UsageKey
from xmodule_django.models import CourseKeyField, UsageKeyField


log = logging.getLogger(__name__)


# Used to serialize information about a block at the time it was used in
# grade calculation.
BlockRecord = namedtuple('BlockRecord', ['locator', 'weight', 'max_score'])


class BlockRecordList(tuple):
    """
    An immutable ordered list of BlockRecord objects.
    """

    def __new__(cls, blocks):
        return super(BlockRecordList, cls).__new__(cls, tuple(blocks))

    def __init__(self, blocks):
        super(BlockRecordList, self).__init__(blocks)
        self._json = None
        self._hash = None

    def _get_course_key_string(self):
        """
        Get the course key as a string.  All blocks are from the same course,
        so just grab one arbitrarily.  If no blocks are present, return None.
        """
        if self:
            a_block = next(iter(self))
            return unicode(a_block.locator.course_key)
        else:
            return None

    def to_json(self):
        """
        Return a JSON-serialized version of the list of block records, using a
        stable ordering.
        """
        if self._json is None:
            list_of_block_dicts = [block._asdict() for block in self]
            course_key_string = self._get_course_key_string()  # all blocks are from the same course

            for block_dict in list_of_block_dicts:
                block_dict['locator'] = unicode(block_dict['locator'])  # BlockUsageLocator is not json-serializable
            data = {
                'course_key': course_key_string,
                'blocks': list_of_block_dicts,
            }

            self._json = json.dumps(
                data,
                separators=(',', ':'),  # Remove spaces from separators for more compact representation
                sort_keys=True,
            )
        return self._json

    @classmethod
    def from_json(cls, blockrecord_json):
        """
        Return a BlockRecordList from a previously serialized json.
        """
        data = json.loads(blockrecord_json)
        course_key = data['course_key']
        if course_key is not None:
            course_key = CourseKey.from_string(course_key)
        else:
            # If there was no course key, there are no blocks.
            assert len(data['blocks']) == 0
        block_dicts = data['blocks']
        record_generator = (
            BlockRecord(
                locator=UsageKey.from_string(block["locator"]).replace(course_key=course_key),
                weight=block["weight"],
                max_score=block["max_score"],
            )
            for block in block_dicts
        )
        return cls(record_generator)

    @classmethod
    def from_list(cls, blocks):
        """
        Return a BlockRecordList from a list.
        """
        return cls(tuple(blocks))

    def to_hash(self):
        """
        Return a hashed version of the list of block records.

        This currently hashes using sha1, and returns a base64 encoded version
        of the binary digest.  In the future, different algorithms could be
        supported by adding a label indicated which algorithm was used, e.g.,
        "sha256$j0NDRmSPa5bfid2pAcUXaxCm2Dlh3TwayItZstwyeqQ=".
        """
        if self._hash is None:
            self._hash = b64encode(sha1(self.to_json()).digest())
        return self._hash


class VisibleBlocksQuerySet(models.QuerySet):
    """
    A custom QuerySet representing VisibleBlocks.
    """

    def create_from_blockrecords(self, blocks):
        """
        Creates a new VisibleBlocks model object.

        Argument 'blocks' should be a BlockRecordList.
        """
        model, _ = self.get_or_create(hashed=blocks.to_hash(), defaults={'blocks_json': blocks.to_json()})
        return model


class VisibleBlocks(models.Model):
    """
    A django model used to track the state of a set of visible blocks under a
    given subsection at the time they are used for grade calculation.

    This state is represented using an array of BlockRecord, stored
    in the blocks_json field. A hash of this json array is used for lookup
    purposes.
    """
    blocks_json = models.TextField()
    hashed = models.CharField(max_length=100, unique=True)

    objects = VisibleBlocksQuerySet.as_manager()

    def __unicode__(self):
        """
        String representation of this model.
        """
        return u"VisibleBlocks object - hash:{}, raw json:'{}'".format(self.hashed, self.blocks_json)

    @property
    def blocks(self):
        """
        Returns the blocks_json data stored on this model as a list of
        BlockRecords in the order they were provided.
        """
        return BlockRecordList.from_json(self.blocks_json)


class PersistentSubsectionGrade(TimeStampedModel):
    """
    A django model tracking persistent grades at the subsection level.
    """

    class Meta(object):
        unique_together = [
            # * Specific grades can be pulled using all three columns,
            # * Progress page can pull all grades for a given (course_id, user_id)
            # * Course staff can see all grades for a course using (course_id,)
            ('course_id', 'user_id', 'usage_key'),
        ]

    # primary key will need to be large for this table
    id = UnsignedBigIntAutoField(primary_key=True)  # pylint: disable=invalid-name

    # uniquely identify this particular grade object
    user_id = models.IntegerField(blank=False)
    course_id = CourseKeyField(blank=False, max_length=255)

    # note: the usage_key may not have the run filled in for
    # old mongo courses.  Use the full_usage_key property
    # instead when you want to use/compare the usage_key.
    usage_key = UsageKeyField(blank=False, max_length=255)

    # Information relating to the state of content when grade was calculated
    subtree_edited_timestamp = models.DateTimeField('last content edit timestamp', blank=False)
    course_version = models.CharField('guid of latest course version', blank=True, max_length=255)

    # earned/possible refers to the number of points achieved and available to achieve.
    # graded refers to the subset of all problems that are marked as being graded.
    earned_all = models.FloatField(blank=False)
    possible_all = models.FloatField(blank=False)
    earned_graded = models.FloatField(blank=False)
    possible_graded = models.FloatField(blank=False)

    # track which blocks were visible at the time of grade calculation
    visible_blocks = models.ForeignKey(VisibleBlocks, db_column='visible_blocks_hash', to_field='hashed')

    # # use custom manager
    # objects = PersistentSubsectionGradeQuerySet.as_manager()

    @property
    def full_usage_key(self):
        """
        Returns the "correct" usage key value with the run filled in.
        """
        if self.usage_key.run is None:  # pylint: disable=no-member
            return self.usage_key.replace(course_key=self.course_id)
        else:
            return self.usage_key

    def __unicode__(self):
        """
        Returns a string representation of this model.
        """
        return u"{} user: {}, course version: {}, subsection {} ({}). {}/{} graded, {}/{} all".format(
            type(self).__name__,
            self.user_id,
            self.course_version,
            self.usage_key,
            self.visible_blocks.hashed,
            self.earned_graded,
            self.possible_graded,
            self.earned_all,
            self.possible_all,
        )

    @classmethod
    def read_grade(cls, user_id, usage_key):
        """
        Reads a grade from database

        Arguments:
            user_id: The user associated with the desired grade
            usage_key: The location of the subsection associated with the desired grade

        Raises PersistentSubsectionGrade.DoesNotExist if applicable
        """
        return cls.objects.select_related('visible_blocks').get(
            user_id=user_id,
            course_id=usage_key.course_key,  # course_id is included to take advantage of db indexes
            usage_key=usage_key,
        )

    @classmethod
    def read_grades_for_user_in_course(cls, user_id, course_key):
        """
        Reads all grades from database for the given course.

        Arguments:
            user_id: The user associated with the desired grades
            course_key: The course identifier for the desired grades
        """
        return cls.objects.select_related('visible_blocks').filter(
            user_id=user_id,
            course_id=course_key,
        )

    @classmethod
    def _prepare_grade_params(cls, params):
        """
        Prepares the fields for the grade record.
        """
        params['visible_blocks'] = VisibleBlocks.objects.create_from_blockrecords(
            BlockRecordList.from_list(params['visible_blocks'])
        )
        params['course_version'] = params.get('course_version', None) or ""
        if not params.get('course_id', None):
            params['course_id'] = params['usage_key'].course_key

    @classmethod
    def update_or_create_grade(cls, **kwargs):
        """
        Wrapper for create_grade or update_grade, depending on which applies.
        Takes the same arguments as both of those methods.
        """
        cls._prepare_grade_params(kwargs)

        user_id = kwargs.pop('user_id')
        usage_key = kwargs.pop('usage_key')

        grade, _ = cls.objects.update_or_create(
            user_id=user_id,
            course_id=usage_key.course_key,
            usage_key=usage_key,
            defaults=kwargs,
        )
        return grade

    @classmethod
    def create_grade(cls, **kwargs):
        """
        Wrapper for objects.create.
        """
        cls._prepare_grade_params(kwargs)
        return cls.objects.create(**kwargs)

    @classmethod
    def bulk_create_grades(cls, list_of_params):
        """
        Bulk creation of grades.
        """
        if list_of_params:
            for params in list_of_params:
                cls._prepare_grade_params(params)
            return cls.objects.bulk_create([
                PersistentSubsectionGrade(**params)
                for params in list_of_params
            ])
