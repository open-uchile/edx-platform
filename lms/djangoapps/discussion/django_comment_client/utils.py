# pylint: skip-file


import json
import logging
from collections import defaultdict
from datetime import datetime

import six
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.http import HttpResponse
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin
from opaque_keys.edx.keys import CourseKey, i4xEncoder, UsageKey
from pytz import UTC
from six import text_type
from six.moves import map

from lms.djangoapps.courseware import courses
from lms.djangoapps.courseware.access import has_access
from lms.djangoapps.discussion.django_comment_client.constants import TYPE_ENTRY, TYPE_SUBCATEGORY
from lms.djangoapps.discussion.django_comment_client.permissions import (
    check_permissions_by_view,
    get_team,
    has_permission
)
from lms.djangoapps.discussion.django_comment_client.settings import MAX_COMMENT_DEPTH
from openedx.core.djangoapps.course_groups.cohorts import get_cohort_id, get_cohort_names, is_course_cohorted
from openedx.core.djangoapps.django_comment_common.models import (
    FORUM_ROLE_COMMUNITY_TA,
    FORUM_ROLE_STUDENT,
    CourseDiscussionSettings,
    DiscussionsIdMapping,
    Role
)
from openedx.core.djangoapps.django_comment_common.utils import get_course_discussion_settings
from openedx.core.lib.cache_utils import request_cached
from common.djangoapps.student.models import get_user_by_username_or_email
from common.djangoapps.student.roles import GlobalStaff
from xmodule.modulestore.django import modulestore
from xmodule.partitions.partitions import ENROLLMENT_TRACK_PARTITION_ID
from xmodule.partitions.partitions_service import PartitionService

log = logging.getLogger(__name__)


def extract(dic, keys):
    """
    Returns a subset of keys from the provided dictionary
    """
    return {k: dic.get(k) for k in keys}


def strip_none(dic):
    """
    Returns a dictionary stripped of any keys having values of None
    """
    return dict([(k, v) for k, v in six.iteritems(dic) if v is not None])


def strip_blank(dic):
    """
    Returns a dictionary stripped of any 'blank' (empty) keys
    """
    def _is_blank(v):
        """
        Determines if the provided value contains no information
        """
        return isinstance(v, str) and len(v.strip()) == 0
    return dict([(k, v) for k, v in six.iteritems(dic) if not _is_blank(v)])

# TODO should we be checking if d1 and d2 have the same keys with different values?


def get_role_ids(course_id):
    """
    Returns a dictionary having role names as keys and a list of users as values
    """
    ### EOL ###
    from common.djangoapps.student.models import CourseAccessRole
    course_roles = CourseAccessRole.objects.filter(course_id=course_id, role__in=['staff', 'instructor']).values('user')
    course_roles = [x['user'] for x in course_roles]
    roles = Role.objects.filter(course_id=course_id).exclude(name=FORUM_ROLE_STUDENT)
    response = dict([(role.name, list(role.users.values_list('id', flat=True))) for role in roles])
    response['Administrator'] = response['Administrator'] + course_roles
    response['Administrator'] = list(dict.fromkeys(response['Administrator']))
    return response


def has_discussion_privileges(user, course_id):
    """
    Returns True if the user is privileged in teams discussions for
    this course. The user must be one of Discussion Admin, Moderator,
    or Community TA.

    Args:
      user (User): The user to check privileges for.
      course_id (CourseKey): A key for the course to check privileges for.

    Returns:
      bool
    """
    # get_role_ids returns a dictionary of only admin, moderator and community TAs.
    roles = get_role_ids(course_id)
    for role in roles:
        if user.id in roles[role]:
            return True
    return False


def has_forum_access(uname, course_id, rolename):
    """
    Boolean operation which tests a user's role-based permissions (not actually forums-specific)
    """
    try:
        role = Role.objects.get(name=rolename, course_id=course_id)
    except Role.DoesNotExist:
        return False
    return role.users.filter(username=uname).exists()


def is_user_community_ta(user, course_id):
    """
    Boolean operation to check whether a user's role is Community TA or not
    """
    return has_forum_access(user, course_id, FORUM_ROLE_COMMUNITY_TA)


def has_required_keys(xblock):
    """
    Returns True iff xblock has the proper attributes for generating metadata
    with get_discussion_id_map_entry()
    """
    for key in ('discussion_id', 'discussion_category', 'discussion_target'):
        if getattr(xblock, key, None) is None:
            log.debug(
                u"Required key '%s' not in discussion %s, leaving out of category map",
                key,
                xblock.location
            )
            return False
    return True


def get_accessible_discussion_xblocks(course, user, include_all=False):
    """
    Return a list of all valid discussion xblocks in this course that
    are accessible to the given user.
    """
    include_all = getattr(user, 'is_community_ta', False)
    return get_accessible_discussion_xblocks_by_course_id(course.id, user, include_all=include_all)


@request_cached()
def get_accessible_discussion_xblocks_by_course_id(course_id, user=None, include_all=False):  # pylint: disable=invalid-name
    """
    Return a list of all valid discussion xblocks in this course.
    Checks for the given user's access if include_all is False.
    """
    all_xblocks = modulestore().get_items(course_id, qualifiers={'category': 'discussion'}, include_orphans=False)
    all_xblocks2= modulestore().get_items(course_id, qualifiers={'category': 'eoldiscussion'}, include_orphans=False)
    aux1 = [
        xblock for xblock in all_xblocks
        if has_required_keys(xblock) and (include_all or has_access(user, 'load', xblock, course_id))
    ]
    aux2 = [
        xblock for xblock in all_xblocks2
        if has_required_keys(xblock) and (include_all or has_access(user, 'load', xblock, course_id))
    ]
    return aux1 + aux2


def get_discussion_id_map_entry(xblock):
    """
    Returns a tuple of (discussion_id, metadata) suitable for inclusion in the results of get_discussion_id_map().
    """
    return (
        xblock.discussion_id,
        {
            "location": xblock.location,
            "title": xblock.discussion_category.split("/")[-1].strip() + (" / " + xblock.discussion_target if xblock.discussion_target else "")
        }
    )


class DiscussionIdMapIsNotCached(Exception):
    """Thrown when the discussion id map is not cached for this course, but an attempt was made to access it."""
    pass


@request_cached()
def get_cached_discussion_key(course_id, discussion_id):
    """
    Returns the usage key of the discussion xblock associated with discussion_id if it is cached. If the discussion id
    map is cached but does not contain discussion_id, returns None. If the discussion id map is not cached for course,
    raises a DiscussionIdMapIsNotCached exception.
    """
    try:
        mapping = DiscussionsIdMapping.objects.get(course_id=course_id).mapping
        if not mapping:
            raise DiscussionIdMapIsNotCached()

        usage_key_string = mapping.get(discussion_id)
        if usage_key_string:
            return UsageKey.from_string(usage_key_string).map_into_course(course_id)
        else:
            return None
    except DiscussionsIdMapping.DoesNotExist:
        raise DiscussionIdMapIsNotCached()


def get_cached_discussion_id_map(course, discussion_ids, user):
    """
    Returns a dict mapping discussion_ids to respective discussion xblock metadata if it is cached and visible to the
    user. If not, returns the result of get_discussion_id_map
    """
    return get_cached_discussion_id_map_by_course_id(course.id, discussion_ids, user)


def get_cached_discussion_id_map_by_course_id(course_id, discussion_ids, user):
    """
    Returns a dict mapping discussion_ids to respective discussion xblock metadata if it is cached and visible to the
    user. If not, returns the result of get_discussion_id_map
    """
    include_all = getattr(user, 'is_community_ta', False)
    try:
        entries = []
        for discussion_id in discussion_ids:
            key = get_cached_discussion_key(course_id, discussion_id)
            if not key:
                continue
            xblock = _get_item_from_modulestore(key)
            if not (has_required_keys(xblock) and (include_all or has_access(user, 'load', xblock, course_id))):
                continue
            entries.append(get_discussion_id_map_entry(xblock))
        return dict(entries)
    except DiscussionIdMapIsNotCached:
        return get_discussion_id_map_by_course_id(course_id, user)


def get_discussion_id_map(course, user):
    """
    Transform the list of this course's discussion xblocks (visible to a given user) into a dictionary of metadata keyed
    by discussion_id.
    """
    return get_discussion_id_map_by_course_id(course.id, user)


def get_discussion_id_map_by_course_id(course_id, user):
    """
    Transform the list of this course's discussion xblocks (visible to a given user) into a dictionary of metadata keyed
    by discussion_id.
    """
    xblocks = get_accessible_discussion_xblocks_by_course_id(course_id, user)
    return dict(list(map(get_discussion_id_map_entry, xblocks)))


@request_cached()
def _get_item_from_modulestore(key):
    return modulestore().get_item(key)


def _filter_unstarted_categories(category_map, course):
    """
    Returns a subset of categories from the provided map which have not yet met the start date
    Includes information about category children, subcategories (different), and entries
    """
    now = datetime.now(UTC)

    result_map = {}

    unfiltered_queue = [category_map]
    filtered_queue = [result_map]

    while unfiltered_queue:

        unfiltered_map = unfiltered_queue.pop()
        filtered_map = filtered_queue.pop()

        filtered_map["children"] = []
        filtered_map["entries"] = {}
        filtered_map["subcategories"] = {}

        for child, c_type in unfiltered_map["children"]:
            if child in unfiltered_map["entries"] and c_type == TYPE_ENTRY:
                if course.self_paced or unfiltered_map["entries"][child]["start_date"] <= now:
                    filtered_map["children"].append((child, c_type))
                    filtered_map["entries"][child] = {}
                    for key in unfiltered_map["entries"][child]:
                        if key != "start_date":
                            filtered_map["entries"][child][key] = unfiltered_map["entries"][child][key]
                else:
                    log.debug(
                        u"Filtering out:%s with start_date: %s", child, unfiltered_map["entries"][child]["start_date"]
                    )
            else:
                if course.self_paced or unfiltered_map["subcategories"][child]["start_date"] < now:
                    filtered_map["children"].append((child, c_type))
                    filtered_map["subcategories"][child] = {}
                    unfiltered_queue.append(unfiltered_map["subcategories"][child])
                    filtered_queue.append(filtered_map["subcategories"][child])

    return result_map


def _sort_map_entries(category_map, sort_alpha):
    """
    Internal helper method to list category entries according to the provided sort order
    """
    things = []
    for title, entry in category_map["entries"].items():
        if entry["sort_key"] is None and sort_alpha:
            entry["sort_key"] = title
        things.append((title, entry, TYPE_ENTRY))
    for title, category in category_map["subcategories"].items():
        things.append((title, category, TYPE_SUBCATEGORY))
        _sort_map_entries(category_map["subcategories"][title], sort_alpha)
    key_method = lambda x: x[1]["sort_key"] if x[1]["sort_key"] is not None else ''
    category_map["children"] = [(x[0], x[2]) for x in sorted(things, key=key_method)]


def get_discussion_category_map(course, user, divided_only_if_explicit=False, exclude_unstarted=True):
    """
    Transform the list of this course's discussion xblocks into a recursive dictionary structure.  This is used
    to render the discussion category map in the discussion tab sidebar for a given user.

    Args:
        course: Course for which to get the ids.
        user:  User to check for access.
        divided_only_if_explicit (bool): If True, inline topics are marked is_divided only if they are
            explicitly listed in CourseDiscussionSettings.discussion_topics.

    Example:
        >>> example = {
        >>>               "entries": {
        >>>                   "General": {
        >>>                       "sort_key": "General",
        >>>                       "is_divided": True,
        >>>                       "id": "i4x-edx-eiorguegnru-course-foobarbaz"
        >>>                   }
        >>>               },
        >>>               "children": [
        >>>                     ["General", "entry"],
        >>>                     ["Getting Started", "subcategory"]
        >>>               ],
        >>>               "subcategories": {
        >>>                   "Getting Started": {
        >>>                       "subcategories": {},
        >>>                       "children": [
        >>>                           ["Working with Videos", "entry"],
        >>>                           ["Videos on edX", "entry"]
        >>>                       ],
        >>>                       "entries": {
        >>>                           "Working with Videos": {
        >>>                               "sort_key": None,
        >>>                               "is_divided": False,
        >>>                               "id": "d9f970a42067413cbb633f81cfb12604"
        >>>                           },
        >>>                           "Videos on edX": {
        >>>                               "sort_key": None,
        >>>                               "is_divided": False,
        >>>                               "id": "98d8feb5971041a085512ae22b398613"
        >>>                           }
        >>>                       }
        >>>                   }
        >>>               }
        >>>          }

    """
    unexpanded_category_map = defaultdict(list)

    xblocks = get_accessible_discussion_xblocks(course, user)

    discussion_settings = get_course_discussion_settings(course.id)
    discussion_division_enabled = course_discussion_division_enabled(discussion_settings)
    divided_discussion_ids = discussion_settings.divided_discussions

    for xblock in xblocks:
        discussion_id = xblock.discussion_id
        title = xblock.discussion_target
        sort_key = xblock.sort_key
        category = " / ".join([x.strip() for x in xblock.discussion_category.split("/")])
        # Handle case where xblock.start is None
        entry_start_date = xblock.start if xblock.start else datetime.max.replace(tzinfo=UTC)
        unexpanded_category_map[category].append({"title": title,
                                                  "id": discussion_id,
                                                  "sort_key": sort_key,
                                                  "start_date": entry_start_date})

    category_map = {"entries": defaultdict(dict), "subcategories": defaultdict(dict)}
    for category_path, entries in unexpanded_category_map.items():
        node = category_map["subcategories"]
        path = [x.strip() for x in category_path.split("/")]

        # Find the earliest start date for the entries in this category
        category_start_date = None
        for entry in entries:
            if category_start_date is None or entry["start_date"] < category_start_date:
                category_start_date = entry["start_date"]

        for level in path[:-1]:
            if level not in node:
                node[level] = {"subcategories": defaultdict(dict),
                               "entries": defaultdict(dict),
                               "sort_key": level,
                               "start_date": category_start_date}
            else:
                if node[level]["start_date"] > category_start_date:
                    node[level]["start_date"] = category_start_date
            node = node[level]["subcategories"]

        level = path[-1]
        if level not in node:
            node[level] = {"subcategories": defaultdict(dict),
                           "entries": defaultdict(dict),
                           "sort_key": level,
                           "start_date": category_start_date}
        else:
            if node[level]["start_date"] > category_start_date:
                node[level]["start_date"] = category_start_date

        divide_all_inline_discussions = (
            not divided_only_if_explicit and discussion_settings.always_divide_inline_discussions
        )
        dupe_counters = defaultdict(lambda: 0)  # counts the number of times we see each title
        for entry in entries:
            is_entry_divided = (
                discussion_division_enabled and (
                    divide_all_inline_discussions or entry["id"] in divided_discussion_ids
                )
            )

            title = entry["title"]
            if node[level]["entries"][title]:
                # If we've already seen this title, append an incrementing number to disambiguate
                # the category from other categores sharing the same title in the course discussion UI.
                dupe_counters[title] += 1
                title = u"{title} ({counter})".format(title=title, counter=dupe_counters[title])
            node[level]["entries"][title] = {"id": entry["id"],
                                             "sort_key": entry["sort_key"],
                                             "start_date": entry["start_date"],
                                             "is_divided": is_entry_divided}

    # TODO.  BUG! : course location is not unique across multiple course runs!
    # (I think Kevin already noticed this)  Need to send course_id with requests, store it
    # in the backend.
    for topic, entry in course.discussion_topics.items():
        category_map['entries'][topic] = {
            "id": entry["id"],
            "sort_key": entry.get("sort_key", topic),
            "start_date": datetime.now(UTC),
            "is_divided": (
                discussion_division_enabled and entry["id"] in divided_discussion_ids
            )
        }

    _sort_map_entries(category_map, course.discussion_sort_alpha)

    return _filter_unstarted_categories(category_map, course) if exclude_unstarted else category_map


def discussion_category_id_access(course, user, discussion_id, xblock=None):
    """
    Returns True iff the given discussion_id is accessible for user in course.
    Assumes that the commentable identified by discussion_id has a null or 'course' context.
    Uses the discussion id cache if available, falling back to
    get_discussion_categories_ids if there is no cache.
    """
    include_all = getattr(user, 'is_community_ta', False)
    if discussion_id in course.top_level_discussion_topic_ids:
        return True
    try:
        if not xblock:
            key = get_cached_discussion_key(course.id, discussion_id)
            if not key:
                return False
            xblock = _get_item_from_modulestore(key)
        return has_required_keys(xblock) and (include_all or has_access(user, 'load', xblock, course.id))
    except DiscussionIdMapIsNotCached:
        return discussion_id in get_discussion_categories_ids(course, user)


def get_discussion_categories_ids(course, user, include_all=False):
    """
    Returns a list of available ids of categories for the course that
    are accessible to the given user.

    Args:
        course: Course for which to get the ids.
        user:  User to check for access.
        include_all (bool): If True, return all ids. Used by configuration views.

    """
    accessible_discussion_ids = [
        xblock.discussion_id for xblock in get_accessible_discussion_xblocks(course, user, include_all=include_all)
    ]
    return course.top_level_discussion_topic_ids + accessible_discussion_ids


class JsonResponse(HttpResponse):
    """
    Django response object delivering JSON representations
    """
    def __init__(self, data=None):
        """
        Object constructor, converts data (if provided) to JSON
        """
        content = json.dumps(data, cls=i4xEncoder)
        super(JsonResponse, self).__init__(content,
                                           content_type='application/json; charset=utf-8')


class JsonError(HttpResponse):
    """
    Django response object delivering JSON exceptions
    """
    def __init__(self, error_messages=[], status=400):
        """
        Object constructor, returns an error response containing the provided exception messages
        """
        if isinstance(error_messages, six.string_types):
            error_messages = [error_messages]
        content = json.dumps({'errors': error_messages}, indent=2, ensure_ascii=False)
        super(JsonError, self).__init__(content,
                                        content_type='application/json; charset=utf-8', status=status)


class HtmlResponse(HttpResponse):
    """
    Django response object delivering HTML representations
    """
    def __init__(self, html=''):
        """
        Object constructor, brokers provided HTML to caller
        """
        super(HtmlResponse, self).__init__(html, content_type='text/plain')


class ViewNameMiddleware(MiddlewareMixin):
    """
    Django middleware object to inject view name into request context
    """
    def process_view(self, request, view_func, view_args, view_kwargs):
        """
        Injects the view name value into the request context
        """
        request.view_name = view_func.__name__


class QueryCountDebugMiddleware(MiddlewareMixin):
    """
    This middleware will log the number of queries run
    and the total time taken for each request (with a
    status code of 200). It does not currently support
    multi-db setups.
    """
    def process_response(self, request, response):
        """
        Log information for 200 OK responses as part of the outbound pipeline
        """
        if response.status_code == 200:
            total_time = 0

            for query in connection.queries:
                query_time = query.get('time')
                if query_time is None:
                    # django-debug-toolbar monkeypatches the connection
                    # cursor wrapper and adds extra information in each
                    # item in connection.queries. The query time is stored
                    # under the key "duration" rather than "time" and is
                    # in milliseconds, not seconds.
                    query_time = query.get('duration', 0) / 1000
                total_time += float(query_time)

            log.info(u'%s queries run, total %s seconds', len(connection.queries), total_time)
        return response


def get_ability(course_id, content, user):
    """
    Return a dictionary of forums-oriented actions and the user's permission to perform them
    """
    (user_group_id, content_user_group_id) = get_user_group_ids(course_id, content, user)
    return {
        'editable': check_permissions_by_view(
            user,
            course_id,
            content,
            "update_thread" if content['type'] == 'thread' else "update_comment",
            user_group_id,
            content_user_group_id
        ),
        'can_reply': check_permissions_by_view(
            user, course_id, content, "create_comment" if content['type'] == 'thread' else "create_sub_comment",
        ),
        'can_delete': check_permissions_by_view(
            user,
            course_id,
            content,
            "delete_thread" if content['type'] == 'thread' else "delete_comment",
            user_group_id,
            content_user_group_id
        ),
        'can_openclose': check_permissions_by_view(
            user,
            course_id,
            content,
            "openclose_thread" if content['type'] == 'thread' else False,
            user_group_id,
            content_user_group_id
        ),
        'can_vote': not is_content_authored_by(content, user) and check_permissions_by_view(
            user,
            course_id,
            content,
            "vote_for_thread" if content['type'] == 'thread' else "vote_for_comment"
        ),
        'can_report': not is_content_authored_by(content, user) and (check_permissions_by_view(
            user,
            course_id,
            content,
            "flag_abuse_for_thread" if content['type'] == 'thread' else "flag_abuse_for_comment"
        ) or GlobalStaff().has_user(user))
    }

# TODO: RENAME


def get_user_group_ids(course_id, content, user=None):
    """
    Given a user, course ID, and the content of the thread or comment, returns the group ID for the current user
    and the user that posted the thread/comment.
    """
    content_user_group_id = None
    user_group_id = None
    if course_id is not None:
        if content.get('username'):
            try:
                content_user = get_user_by_username_or_email(content.get('username'))
                content_user_group_id = get_group_id_for_user_from_cache(content_user, course_id)
            except User.DoesNotExist:
                content_user_group_id = None

        user_group_id = get_group_id_for_user_from_cache(user, course_id) if user else None
    return user_group_id, content_user_group_id


def get_annotated_content_info(course_id, content, user, user_info):
    """
    Get metadata for an individual content (thread or comment)
    """
    voted = ''
    if content['id'] in user_info['upvoted_ids']:
        voted = 'up'
    elif content['id'] in user_info['downvoted_ids']:
        voted = 'down'
    return {
        'voted': voted,
        'subscribed': content['id'] in user_info['subscribed_thread_ids'],
        'ability': get_ability(course_id, content, user),
    }

# TODO: RENAME


def get_annotated_content_infos(course_id, thread, user, user_info):
    """
    Get metadata for a thread and its children
    """
    infos = {}

    def annotate(content):
        infos[str(content['id'])] = get_annotated_content_info(course_id, content, user, user_info)
        for child in (
                content.get('children', []) +
                content.get('endorsed_responses', []) +
                content.get('non_endorsed_responses', [])
        ):
            annotate(child)
    annotate(thread)
    return infos


def get_metadata_for_threads(course_id, threads, user, user_info):
    """
    Returns annotated content information for the specified course, threads, and user information
    """

    def infogetter(thread):
        return get_annotated_content_infos(course_id, thread, user, user_info)

    metadata = {}
    for thread in threads:
        metadata.update(infogetter(thread))
    return metadata


def permalink(content):
    if isinstance(content['course_id'], CourseKey):
        course_id = text_type(content['course_id'])
    else:
        course_id = content['course_id']
    if content['type'] == 'thread':
        return reverse('single_thread', args=[course_id, content['commentable_id'], content['id']])
    else:
        return reverse('single_thread',
                       args=[course_id, content['commentable_id'], content['thread_id']]) + '#' + content['id']


def extend_content(content):
    roles = {}
    if content.get('user_id'):
        try:
            user = User.objects.get(pk=content['user_id'])
            roles = dict(('name', role.name.lower()) for role in user.roles.filter(course_id=content['course_id']))
        except User.DoesNotExist:
            log.error(
                u'User ID %s in comment content %s but not in our DB.',
                content.get('user_id'),
                content.get('id')
            )

    content_info = {
        'displayed_title': content.get('highlighted_title') or content.get('title', ''),
        'displayed_body': content.get('highlighted_body') or content.get('body', ''),
        'permalink': permalink(content),
        'roles': roles,
        'updated': content['created_at'] != content['updated_at'],
    }
    content.update(content_info)
    return content


def add_courseware_context(content_list, course, user, id_map=None):
    """
    Decorates `content_list` with courseware metadata using the discussion id map cache if available.
    """
    if id_map is None:
        id_map = get_cached_discussion_id_map(
            course,
            [content['commentable_id'] for content in content_list],
            user
        )

    for content in content_list:
        commentable_id = content['commentable_id']
        if commentable_id in id_map:
            location = text_type(id_map[commentable_id]["location"])
            title = id_map[commentable_id]["title"]

            url = reverse('jump_to', kwargs={"course_id": text_type(course.id),
                          "location": location})

            content.update({"courseware_url": url, "courseware_title": title})


def prepare_content(content, course_key, is_staff=False, discussion_division_enabled=None, group_names_by_id=None):
    """
    This function is used to pre-process thread and comment models in various
    ways before adding them to the HTTP response.  This includes fixing empty
    attribute fields, enforcing author anonymity, and enriching metadata around
    group ownership and response endorsement.

    @TODO: not all response pre-processing steps are currently integrated into
    this function.

    Arguments:
        content (dict): A thread or comment.
        course_key (CourseKey): The course key of the course.
        is_staff (bool): Whether the user is a staff member.
        discussion_division_enabled (bool): Whether division of course discussions is enabled.
           Note that callers of this method do not need to provide this value (it defaults to None)--
           it is calculated and then passed to recursive calls of this method.
    """
    fields = [
        'id', 'title', 'body', 'course_id', 'anonymous', 'anonymous_to_peers',
        'endorsed', 'parent_id', 'thread_id', 'votes', 'closed', 'created_at',
        'updated_at', 'depth', 'type', 'commentable_id', 'comments_count',
        'at_position_list', 'children', 'highlighted_title', 'highlighted_body',
        'courseware_title', 'courseware_url', 'unread_comments_count',
        'read', 'group_id', 'group_name', 'pinned', 'abuse_flaggers',
        'stats', 'resp_skip', 'resp_limit', 'resp_total', 'thread_type',
        'endorsed_responses', 'non_endorsed_responses', 'non_endorsed_resp_total',
        'endorsement', 'context', 'last_activity_at'
    ]

    if (content.get('anonymous') is False) and ((content.get('anonymous_to_peers') is False) or is_staff):
        fields += ['username', 'user_id']

    content = strip_none(extract(content, fields))

    if content.get("endorsement"):
        endorsement = content["endorsement"]
        endorser = None
        if endorsement["user_id"]:
            try:
                endorser = User.objects.get(pk=endorsement["user_id"])
            except User.DoesNotExist:
                log.error(
                    u"User ID %s in endorsement for comment %s but not in our DB.",
                    content.get('user_id'),
                    content.get('id')
                )

        # Only reveal endorser if requester can see author or if endorser is staff
        if (
                endorser and
                ("username" in fields or has_permission(endorser, "endorse_comment", course_key))
        ):
            endorsement["username"] = endorser.username
        else:
            del endorsement["user_id"]

    if discussion_division_enabled is None:
        discussion_division_enabled = course_discussion_division_enabled(get_course_discussion_settings(course_key))

    for child_content_key in ["children", "endorsed_responses", "non_endorsed_responses"]:
        if child_content_key in content:
            children = [
                prepare_content(
                    child,
                    course_key,
                    is_staff,
                    discussion_division_enabled=discussion_division_enabled,
                    group_names_by_id=group_names_by_id
                )
                for child in content[child_content_key]
            ]
            content[child_content_key] = children

    if discussion_division_enabled:
        # Augment the specified thread info to include the group name if a group id is present.
        if content.get('group_id') is not None:
            course_discussion_settings = get_course_discussion_settings(course_key)
            if group_names_by_id:
                content['group_name'] = group_names_by_id.get(content.get('group_id'))
            else:
                content['group_name'] = get_group_name(content.get('group_id'), course_discussion_settings)
            content['is_commentable_divided'] = is_commentable_divided(
                course_key, content['commentable_id'], course_discussion_settings
            )
    else:
        # Remove any group information that might remain if the course had previously been divided.
        content.pop('group_id', None)

    return content


def get_group_id_for_comments_service(request, course_key, commentable_id=None):
    """
    Given a user requesting content within a `commentable_id`, determine the
    group_id which should be passed to the comments service.

    Returns:
        int: the group_id to pass to the comments service or None if nothing
        should be passed

    Raises:
        ValueError if the requested group_id is invalid
    """
    course_discussion_settings = get_course_discussion_settings(course_key)
    if commentable_id is None or is_commentable_divided(course_key, commentable_id, course_discussion_settings):
        if request.method == "GET":
            requested_group_id = request.GET.get('group_id')
        elif request.method == "POST":
            requested_group_id = request.POST.get('group_id')
        if has_permission(request.user, "see_all_cohorts", course_key):
            if not requested_group_id:
                return None
            group_id = int(requested_group_id)
            _verify_group_exists(group_id, course_discussion_settings)
        else:
            # regular users always query with their own id.
            group_id = get_group_id_for_user_from_cache(request.user, course_key)
        return group_id
    else:
        # Never pass a group_id to the comments service for a non-divided
        # commentable
        return None


@request_cached()
def get_group_id_for_user_from_cache(user, course_id):
    """
    Caches the results of get_group_id_for_user, but serializes the course_id
    instead of the course_discussions_settings object as cache keys.
    """
    return get_group_id_for_user(user, get_course_discussion_settings(course_id))


def get_group_id_for_user(user, course_discussion_settings):
    """
    Given a user, return the group_id for that user according to the course_discussion_settings.
    If discussions are not divided, this method will return None.
    It will also return None if the user is in no group within the specified division_scheme.
    """
    division_scheme = _get_course_division_scheme(course_discussion_settings)
    if division_scheme == CourseDiscussionSettings.COHORT:
        return get_cohort_id(user, course_discussion_settings.course_id)
    elif division_scheme == CourseDiscussionSettings.ENROLLMENT_TRACK:
        partition_service = PartitionService(course_discussion_settings.course_id)
        group_id = partition_service.get_user_group_id_for_partition(user, ENROLLMENT_TRACK_PARTITION_ID)
        # We negate the group_ids from dynamic partitions so that they will not conflict
        # with cohort IDs (which are an auto-incrementing integer field, starting at 1).
        return -1 * group_id if group_id is not None else None
    else:
        return None


def is_comment_too_deep(parent):
    """
    Determine whether a comment with the given parent violates MAX_COMMENT_DEPTH

    parent can be None to determine whether root comments are allowed
    """
    return (
        MAX_COMMENT_DEPTH is not None and (
            MAX_COMMENT_DEPTH < 0 or
            (parent and (parent["depth"] or 0) >= MAX_COMMENT_DEPTH)
        )
    )


def is_commentable_divided(course_key, commentable_id, course_discussion_settings=None):
    """
    Args:
        course_key: CourseKey
        commentable_id: string
        course_discussion_settings: CourseDiscussionSettings model instance (optional). If not
            supplied, it will be retrieved via the course_key.

    Returns:
        Bool: is this commentable divided, meaning that learners are divided into
        groups (either Cohorts or Enrollment Tracks) and only see posts within their group?

    Raises:
        Http404 if the course doesn't exist.
    """
    if not course_discussion_settings:
        course_discussion_settings = get_course_discussion_settings(course_key)

    course = courses.get_course_by_id(course_key)

    if not course_discussion_division_enabled(course_discussion_settings) or get_team(commentable_id):
        # this is the easy case :)
        ans = False
    elif (
        commentable_id in course.top_level_discussion_topic_ids or
        course_discussion_settings.always_divide_inline_discussions is False
    ):
        # top level discussions have to be manually configured as divided
        # (default is not).
        # Same thing for inline discussions if the default is explicitly set to False in settings
        ans = commentable_id in course_discussion_settings.divided_discussions
    else:
        # inline discussions are divided by default
        ans = True

    log.debug(u"is_commentable_divided(%s, %s) = {%s}", course_key, commentable_id, ans)
    return ans


def course_discussion_division_enabled(course_discussion_settings):
    """
    Are discussions divided for the course represented by this instance of
    course_discussion_settings? This method looks both at
    course_discussion_settings.division_scheme, and information about the course
    state itself (For example, are cohorts enabled? And are there multiple
    enrollment tracks?).

    Args:
        course_discussion_settings: CourseDiscussionSettings model instance

    Returns: True if discussion division is enabled for the course, else False
    """
    return _get_course_division_scheme(course_discussion_settings) != CourseDiscussionSettings.NONE


def available_division_schemes(course_key):
    """
    Returns a list of possible discussion division schemes for this course.
    This takes into account if cohorts are enabled and if there are multiple
    enrollment tracks. If no schemes are available, returns an empty list.
    Args:
        course_key: CourseKey

    Returns: list of possible division schemes (for example, CourseDiscussionSettings.COHORT)
    """
    available_schemes = []
    if is_course_cohorted(course_key):
        available_schemes.append(CourseDiscussionSettings.COHORT)
    if enrollment_track_group_count(course_key) > 1:
        available_schemes.append(CourseDiscussionSettings.ENROLLMENT_TRACK)
    return available_schemes


def enrollment_track_group_count(course_key):
    """
    Returns the count of possible enrollment track division schemes for this course.
    Args:
        course_key: CourseKey
    Returns:
        Count of enrollment track division scheme
    """
    return len(_get_enrollment_track_groups(course_key))


def _get_course_division_scheme(course_discussion_settings):
    division_scheme = course_discussion_settings.division_scheme
    if (
        division_scheme == CourseDiscussionSettings.COHORT and
        not is_course_cohorted(course_discussion_settings.course_id)
    ):
        division_scheme = CourseDiscussionSettings.NONE
    elif (
        division_scheme == CourseDiscussionSettings.ENROLLMENT_TRACK and
        enrollment_track_group_count(course_discussion_settings.course_id) <= 1
    ):
        division_scheme = CourseDiscussionSettings.NONE
    return division_scheme


def get_group_name(group_id, course_discussion_settings):
    """
    Given a specified comments_service group_id, returns the learner-facing
    name of the Group. If no such Group exists for the specified group_id
    (taking into account the division_scheme and course specified by course_discussion_settings),
    returns None.
    Args:
        group_id: the group_id as used by the comments_service code
        course_discussion_settings: CourseDiscussionSettings model instance

    Returns: learner-facing name of the Group, or None if no such group exists
    """
    group_names_by_id = get_group_names_by_id(course_discussion_settings)
    return group_names_by_id[group_id] if group_id in group_names_by_id else None


def get_group_names_by_id(course_discussion_settings):
    """
    Creates of a dict of group_id to learner-facing group names, for the division_scheme
    in use as specified by course_discussion_settings.
    Args:
        course_discussion_settings: CourseDiscussionSettings model instance

    Returns: dict of group_id to learner-facing group names. If no division_scheme
    is in use, returns an empty dict.
    """
    division_scheme = _get_course_division_scheme(course_discussion_settings)
    course_key = course_discussion_settings.course_id
    if division_scheme == CourseDiscussionSettings.COHORT:
        return get_cohort_names(courses.get_course_by_id(course_key))
    elif division_scheme == CourseDiscussionSettings.ENROLLMENT_TRACK:
        # We negate the group_ids from dynamic partitions so that they will not conflict
        # with cohort IDs (which are an auto-incrementing integer field, starting at 1).
        return {-1 * group.id: group.name for group in _get_enrollment_track_groups(course_key)}
    else:
        return {}


def _get_enrollment_track_groups(course_key):
    """
    Helper method that returns an array of the Groups in the EnrollmentTrackUserPartition for the given course.
    If no such partition exists on the course, an empty array is returned.
    """
    partition_service = PartitionService(course_key)
    partition = partition_service.get_user_partition(ENROLLMENT_TRACK_PARTITION_ID)
    return partition.groups if partition else []


def _verify_group_exists(group_id, course_discussion_settings):
    """
    Helper method that verifies the given group_id corresponds to a Group in the
    division scheme being used. If it does not, a ValueError will be raised.
    """
    if get_group_name(group_id, course_discussion_settings) is None:
        raise ValueError


def is_discussion_enabled(course_id):
    """
    Return True if discussions are enabled; else False
    """
    return settings.FEATURES.get('ENABLE_DISCUSSION_SERVICE')


def is_content_authored_by(content, user):
    """
    Return True if the author is this content is the passed user, else False
    """
    try:
        return int(content.get('user_id')) == user.id
    except (ValueError, TypeError):
        return False