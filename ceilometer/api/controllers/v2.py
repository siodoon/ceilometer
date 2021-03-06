# -*- encoding: utf-8 -*-
#
# Copyright © 2012 New Dream Network, LLC (DreamHost)
# Copyright 2013 IBM Corp.
#
# Author: Doug Hellmann <doug.hellmann@dreamhost.com>
#         Angus Salkeld <asalkeld@redhat.com>
#         Eoghan Glynn <eglynn@redhat.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Version 2 of the API.
"""

# [GET ] / -- information about this version of the API
#
# [GET   ] /resources -- list the resources
# [GET   ] /resources/<resource> -- information about the resource
# [GET   ] /meters -- list the meters
# [POST  ] /meters -- insert a new sample (and meter/resource if needed)
# [GET   ] /meters/<meter> -- list the samples for this meter
# [PUT   ] /meters/<meter> -- update the meter (not the samples)
# [DELETE] /meters/<meter> -- delete the meter and samples
#
import ast
import base64
import datetime
import inspect
import json
import uuid
import pecan
from pecan import rest

from oslo.config import cfg

import wsme
import wsmeext.pecan as wsme_pecan
from wsme import types as wtypes

from ceilometer.openstack.common import context
from ceilometer.openstack.common.gettextutils import _
from ceilometer.openstack.common import log
from ceilometer.openstack.common import strutils
from ceilometer.openstack.common import timeutils
from ceilometer import sample
from ceilometer import storage
from ceilometer import utils
from ceilometer.api import acl


LOG = log.getLogger(__name__)


ALARM_API_OPTS = [
    cfg.BoolOpt('record_history',
                default=True,
                help='Record alarm change events'
                ),
]

cfg.CONF.register_opts(ALARM_API_OPTS, group='alarm')


operation_kind = wtypes.Enum(str, 'lt', 'le', 'eq', 'ne', 'ge', 'gt')


class _Base(wtypes.Base):

    @classmethod
    def from_db_model(cls, m):
        return cls(**(m.as_dict()))

    @classmethod
    def from_db_and_links(cls, m, links):
        return cls(links=links, **(m.as_dict()))

    def as_dict(self, db_model):
        valid_keys = inspect.getargspec(db_model.__init__)[0]
        if 'self' in valid_keys:
            valid_keys.remove('self')

        return dict((k, getattr(self, k))
                    for k in valid_keys
                    if hasattr(self, k) and
                    getattr(self, k) != wsme.Unset)


class Link(_Base):
    """A link representation
    """

    href = wtypes.text
    "The url of a link"

    rel = wtypes.text
    "The name of a link"

    @classmethod
    def sample(cls):
        return cls(href=('http://localhost:8777/v2/meters/volume?'
                         'q.field=resource_id&'
                         'q.value=bd9431c1-8d69-4ad3-803a-8d4a6b89fd36'),
                   rel='volume'
                   )


class Query(_Base):
    """Sample query filter.
    """

    _op = None  # provide a default

    def get_op(self):
        return self._op or 'eq'

    def set_op(self, value):
        self._op = value

    field = wtypes.text
    "The name of the field to test"

    #op = wsme.wsattr(operation_kind, default='eq')
    # this ^ doesn't seem to work.
    op = wsme.wsproperty(operation_kind, get_op, set_op)
    "The comparison operator. Defaults to 'eq'."

    value = wtypes.text
    "The value to compare against the stored data"

    type = wtypes.text
    "The data type of value to compare against the stored data"

    def __repr__(self):
        # for logging calls
        return '<Query %r %s %r %s>' % (self.field,
                                        self.op,
                                        self.value,
                                        self.type)

    @classmethod
    def sample(cls):
        return cls(field='resource_id',
                   op='eq',
                   value='bd9431c1-8d69-4ad3-803a-8d4a6b89fd36',
                   type='string'
                   )

    def _get_value_as_type(self):
        """Convert metadata value to the specified data type.

        This method is called during metadata query to help convert the
        querying metadata to the data type specified by user. If there is no
        data type given, the metadata will be parsed by ast.literal_eval to
        try to do a smart converting.

        NOTE (flwang) Using "_" as prefix to avoid an InvocationError raised
        from wsmeext/sphinxext.py. It's OK to call it outside the Query class.
        Because the "public" side of that class is actually the outside of the
        API, and the "private" side is the API implementation. The method is
        only used in the API implementation, so it's OK.

        :returns: metadata value converted with the specified data type.
        """
        try:
            converted_value = self.value
            if not self.type:
                try:
                    converted_value = ast.literal_eval(self.value)
                except (ValueError, SyntaxError):
                    msg = _('Failed to convert the metadata value %s'
                            ' automatically') % (self.value)
                    LOG.debug(msg)
            else:
                if self.type == 'integer':
                    converted_value = int(self.value)
                elif self.type == 'float':
                    converted_value = float(self.value)
                elif self.type == 'boolean':
                    converted_value = strutils.bool_from_string(self.value)
                elif self.type == 'string':
                    converted_value = self.value
                else:
                    # For now, this method only support integer, float,
                    # boolean and and string as the metadata type. A TypeError
                    # will be raised for any other type.
                    raise TypeError()
        except ValueError:
            msg = _('Failed to convert the metadata value %(value)s'
                    ' to the expected data type %(type)s.') % \
                {'value': self.value, 'type': self.type}
            raise wsme.exc.ClientSideError(msg)
        except TypeError:
            msg = _('The data type %s is not supported. The supported'
                    ' data type list is: integer, float, boolean and'
                    ' string.') % (self.type)
            raise wsme.exc.ClientSideError(msg)
        except Exception:
            msg = _('Unexpected exception converting %(value)s to'
                    ' the expected data type %(type)s.') % \
                {'value': self.value, 'type': self.type}
            raise wsme.exc.ClientSideError(msg)
        return converted_value


def _sanitize_query(q, valid_keys, headers=None):
    '''Check the query to see if:
    1) the request is coming from admin - then allow full visibility
    2) non-admin - make sure that the query includes the requester's
    project.
    '''
    auth_project = acl.get_limited_to_project(headers or
                                              pecan.request.headers)
    if auth_project:
        proj_q = [i for i in q if i.field == 'project_id']
        for i in proj_q:
            if auth_project != i.value or i.op != 'eq':
                # TODO(asalkeld) in the next version of wsme (0.5b3+)
                # activate this code to be able to return the correct
                # status code (also update api/v2/test_acl.py).
                #return wsme.api.Response([return_type()],
                #                         status_code=401)
                errstr = 'Not Authorized to access project %s %s' % (i.op,
                                                                     i.value)
                raise wsme.exc.ClientSideError(errstr)

        if not proj_q and 'on_behalf_of' not in valid_keys:
            # The user is restricted, but they didn't specify a project
            # so add it for them.
            q.append(Query(field='project_id',
                           op='eq',
                           value=auth_project))
    return q


def _query_to_kwargs(query, db_func, internal_keys=[], headers=None):
    valid_keys = inspect.getargspec(db_func)[0]
    query = _sanitize_query(query, valid_keys, headers=headers)
    internal_keys.append('self')
    valid_keys = set(valid_keys) - set(internal_keys)
    translation = {'user_id': 'user',
                   'project_id': 'project',
                   'resource_id': 'resource'}
    stamp = {}
    metaquery = {}
    kwargs = {}
    for i in query:
        if i.field == 'timestamp':
            if i.op in ('lt', 'le'):
                stamp['end_timestamp'] = i.value
                stamp['end_timestamp_op'] = i.op
            elif i.op in ('gt', 'ge'):
                stamp['start_timestamp'] = i.value
                stamp['start_timestamp_op'] = i.op
            else:
                raise wsme.exc.InvalidInput('op', i.op,
                                            'unimplemented operator for %s' %
                                            i.field)
        else:
            if i.op == 'eq':
                if i.field == 'search_offset':
                    stamp['search_offset'] = i.value
                elif i.field.startswith('metadata.'):
                    metaquery[i.field] = i._get_value_as_type()
                elif i.field.startswith('resource_metadata.'):
                    metaquery[i.field[9:]] = i._get_value_as_type()
                else:
                    key = translation.get(i.field, i.field)
                    if key not in valid_keys:
                        msg = ("unrecognized field in query: %s, "
                               "valid keys: %s") % (query, valid_keys)
                        raise wsme.exc.UnknownArgument(key, msg)
                    kwargs[key] = i.value
            else:
                raise wsme.exc.InvalidInput('op', i.op,
                                            'unimplemented operator for %s' %
                                            i.field)

    if metaquery and 'metaquery' in valid_keys:
        kwargs['metaquery'] = metaquery
    if stamp:
        q_ts = _get_query_timestamps(stamp)
        if 'start' in valid_keys:
            kwargs['start'] = q_ts['query_start']
            kwargs['end'] = q_ts['query_end']
        elif 'start_timestamp' in valid_keys:
            kwargs['start_timestamp'] = q_ts['query_start']
            kwargs['end_timestamp'] = q_ts['query_end']
        else:
            raise wsme.exc.UnknownArgument('timestamp',
                                           "not valid for this resource")
        if 'start_timestamp_op' in stamp:
            kwargs['start_timestamp_op'] = stamp['start_timestamp_op']
        if 'end_timestamp_op' in stamp:
            kwargs['end_timestamp_op'] = stamp['end_timestamp_op']

    return kwargs


def _validate_groupby_fields(groupby_fields):
    """Checks that the list of groupby fields from request is valid and
    if all fields are valid, returns fields with duplicates removed

    """
    # NOTE(terriyu): Currently, metadata fields are not supported in our
    # group by statistics implementation
    valid_fields = set(['user_id', 'resource_id', 'project_id', 'source'])

    invalid_fields = set(groupby_fields) - valid_fields
    if invalid_fields:
        raise wsme.exc.UnknownArgument(invalid_fields,
                                       "Invalid groupby fields")

    # Remove duplicate fields
    # NOTE(terriyu): This assumes that we don't care about the order of the
    # group by fields.
    return list(set(groupby_fields))


def _get_query_timestamps(args={}):
    """Return any optional timestamp information in the request.

    Determine the desired range, if any, from the GET arguments. Set
    up the query range using the specified offset.

    [query_start ... start_timestamp ... end_timestamp ... query_end]

    Returns a dictionary containing:

    query_start: First timestamp to use for query
    start_timestamp: start_timestamp parameter from request
    query_end: Final timestamp to use for query
    end_timestamp: end_timestamp parameter from request
    search_offset: search_offset parameter from request

    """
    search_offset = int(args.get('search_offset', 0))

    start_timestamp = args.get('start_timestamp')
    if start_timestamp:
        start_timestamp = timeutils.parse_isotime(start_timestamp)
        start_timestamp = start_timestamp.replace(tzinfo=None)
        query_start = (start_timestamp -
                       datetime.timedelta(minutes=search_offset))
    else:
        query_start = None

    end_timestamp = args.get('end_timestamp')
    if end_timestamp:
        end_timestamp = timeutils.parse_isotime(end_timestamp)
        end_timestamp = end_timestamp.replace(tzinfo=None)
        query_end = end_timestamp + datetime.timedelta(minutes=search_offset)
    else:
        query_end = None

    return {'query_start': query_start,
            'query_end': query_end,
            'start_timestamp': start_timestamp,
            'end_timestamp': end_timestamp,
            'search_offset': search_offset,
            }


def _flatten_metadata(metadata):
    """Return flattened resource metadata without nested structures
    and with all values converted to unicode strings.
    """
    if metadata:
        return dict((k, unicode(v))
                    for k, v in utils.recursive_keypairs(metadata,
                                                         separator='.')
                    if type(v) not in set([list, set]))
    return {}


def _make_link(rel_name, url, type, type_arg, query=None):
    query_str = ''
    if query:
        query_str = '?q.field=%s&q.value=%s' % (query['field'],
                                                query['value'])
    return Link(href=('%s/v2/%s/%s%s') % (url, type, type_arg, query_str),
                rel=rel_name)


class Sample(_Base):
    """A single measurement for a given meter and resource.
    """

    source = wtypes.text
    "An identity source ID"

    counter_name = wtypes.text
    "The name of the meter"
    # FIXME(dhellmann): Make this meter_name?

    counter_type = wtypes.text
    "The type of the meter (see :ref:`measurements`)"
    # FIXME(dhellmann): Make this meter_type?

    counter_unit = wtypes.text
    "The unit of measure for the value in counter_volume"
    # FIXME(dhellmann): Make this meter_unit?

    counter_volume = float
    "The actual measured value"

    user_id = wtypes.text
    "The ID of the user who last triggered an update to the resource"

    project_id = wtypes.text
    "The ID of the project or tenant that owns the resource"

    resource_id = wtypes.text
    "The ID of the :class:`Resource` for which the measurements are taken"

    timestamp = datetime.datetime
    "UTC date and time when the measurement was made"

    resource_metadata = {wtypes.text: wtypes.text}
    "Arbitrary metadata associated with the resource"

    message_id = wtypes.text
    "A unique identifier for the sample"

    def __init__(self, counter_volume=None, resource_metadata={},
                 timestamp=None, **kwds):
        if counter_volume is not None:
            counter_volume = float(counter_volume)
        resource_metadata = _flatten_metadata(resource_metadata)
        # this is to make it easier for clients to pass a timestamp in
        if timestamp and isinstance(timestamp, basestring):
            timestamp = timeutils.parse_isotime(timestamp)

        super(Sample, self).__init__(counter_volume=counter_volume,
                                     resource_metadata=resource_metadata,
                                     timestamp=timestamp, **kwds)
        # Seems the mandatory option doesn't work so do it manually
        for m in ('counter_volume', 'counter_unit',
                  'counter_name', 'counter_type', 'resource_id'):
            if getattr(self, m) in (wsme.Unset, None):
                raise wsme.exc.MissingArgument(m)

        if self.resource_metadata in (wtypes.Unset, None):
            self.resource_metadata = {}

    @classmethod
    def sample(cls):
        return cls(source='openstack',
                   counter_name='instance',
                   counter_type='gauge',
                   counter_unit='instance',
                   counter_volume=1,
                   resource_id='bd9431c1-8d69-4ad3-803a-8d4a6b89fd36',
                   project_id='35b17138-b364-4e6a-a131-8f3099c5be68',
                   user_id='efd87807-12d2-4b38-9c70-5f5c2ac427ff',
                   timestamp=datetime.datetime.utcnow(),
                   resource_metadata={'name1': 'value1',
                                      'name2': 'value2'},
                   message_id='5460acce-4fd6-480d-ab18-9735ec7b1996',
                   )


class Statistics(_Base):
    """Computed statistics for a query.
    """

    groupby = {wtypes.text: wtypes.text}
    "Dictionary of field names for group, if groupby statistics are requested"

    unit = wtypes.text
    "The unit type of the data set"

    min = float
    "The minimum volume seen in the data"

    max = float
    "The maximum volume seen in the data"

    avg = float
    "The average of all of the volume values seen in the data"

    sum = float
    "The total of all of the volume values seen in the data"

    count = int
    "The number of samples seen"

    duration = float
    "The difference, in seconds, between the oldest and newest timestamp"

    duration_start = datetime.datetime
    "UTC date and time of the earliest timestamp, or the query start time"

    duration_end = datetime.datetime
    "UTC date and time of the oldest timestamp, or the query end time"

    period = int
    "The difference, in seconds, between the period start and end"

    period_start = datetime.datetime
    "UTC date and time of the period start"

    period_end = datetime.datetime
    "UTC date and time of the period end"

    def __init__(self, start_timestamp=None, end_timestamp=None, **kwds):
        super(Statistics, self).__init__(**kwds)
        self._update_duration(start_timestamp, end_timestamp)

    def _update_duration(self, start_timestamp, end_timestamp):
        # "Clamp" the timestamps we return to the original time
        # range, excluding the offset.
        if (start_timestamp and
                self.duration_start and
                self.duration_start < start_timestamp):
            self.duration_start = start_timestamp
            LOG.debug('clamping min timestamp to range')
        if (end_timestamp and
                self.duration_end and
                self.duration_end > end_timestamp):
            self.duration_end = end_timestamp
            LOG.debug('clamping max timestamp to range')

        # If we got valid timestamps back, compute a duration in seconds.
        #
        # If the min > max after clamping then we know the
        # timestamps on the samples fell outside of the time
        # range we care about for the query, so treat them as
        # "invalid."
        #
        # If the timestamps are invalid, return None as a
        # sentinal indicating that there is something "funny"
        # about the range.
        if (self.duration_start and
                self.duration_end and
                self.duration_start <= self.duration_end):
            self.duration = timeutils.delta_seconds(self.duration_start,
                                                    self.duration_end)
        else:
            self.duration_start = self.duration_end = self.duration = None

    @classmethod
    def sample(cls):
        return cls(unit='GiB',
                   min=1,
                   max=9,
                   avg=4.5,
                   sum=45,
                   count=10,
                   duration_start=datetime.datetime(2013, 1, 4, 16, 42),
                   duration_end=datetime.datetime(2013, 1, 4, 16, 47),
                   period=7200,
                   period_start=datetime.datetime(2013, 1, 4, 16, 00),
                   period_end=datetime.datetime(2013, 1, 4, 18, 00),
                   )


class MeterController(rest.RestController):
    """Manages operations on a single meter.
    """
    _custom_actions = {
        'statistics': ['GET'],
    }

    def __init__(self, meter_id):
        pecan.request.context['meter_id'] = meter_id
        self._id = meter_id

    @wsme_pecan.wsexpose([Sample], [Query], int)
    def get_all(self, q=[], limit=None):
        """Return samples for the meter.

        :param q: Filter rules for the data to be returned.
        :param limit: Maximum number of samples to return.
        """
        if limit and limit < 0:
            raise ValueError("Limit must be positive")
        kwargs = _query_to_kwargs(q, storage.SampleFilter.__init__)
        kwargs['meter'] = self._id
        f = storage.SampleFilter(**kwargs)
        return [Sample.from_db_model(e)
                for e in pecan.request.storage_conn.get_samples(f, limit=limit)
                ]

    @wsme.validate([Sample])
    @wsme_pecan.wsexpose([Sample], body=[Sample])
    def post(self, body):
        """Post a list of new Samples to Ceilometer.

        :param body: a list of samples within the request body.
        """
        # Note:
        #  1) the above validate decorator seems to do nothing.
        #  2) the mandatory options seems to also do nothing.
        #  3) the body should already be in a list of Sample's

        samples = [Sample(**b) for b in body]

        now = timeutils.utcnow()
        auth_project = acl.get_limited_to_project(pecan.request.headers)
        def_source = pecan.request.cfg.sample_source
        def_project_id = pecan.request.headers.get('X-Project-Id')
        def_user_id = pecan.request.headers.get('X-User-Id')

        published_samples = []
        for s in samples:
            if self._id != s.counter_name:
                raise wsme.exc.InvalidInput('counter_name', s.counter_name,
                                            'should be %s' % self._id)

            if s.message_id:
                raise wsme.exc.InvalidInput('message_id', s.message_id,
                                            'The message_id must not be set')

            if s.counter_type not in sample.TYPES:
                raise wsme.exc.InvalidInput('counter_type', s.counter_type,
                                            'The counter type must be: ' +
                                            ', '.join(sample.TYPES))

            s.user_id = (s.user_id or def_user_id)
            s.project_id = (s.project_id or def_project_id)
            s.source = '%s:%s' % (s.project_id, (s.source or def_source))
            s.timestamp = (s.timestamp or now)

            if auth_project and auth_project != s.project_id:
                # non admin user trying to cross post to another project_id
                auth_msg = 'can not post samples to other projects'
                raise wsme.exc.InvalidInput('project_id', s.project_id,
                                            auth_msg)

            published_sample = sample.Sample(
                name=s.counter_name,
                type=s.counter_type,
                unit=s.counter_unit,
                volume=s.counter_volume,
                user_id=s.user_id,
                project_id=s.project_id,
                resource_id=s.resource_id,
                timestamp=s.timestamp.isoformat(),
                resource_metadata=s.resource_metadata,
                source=s.source)
            published_samples.append(published_sample)

            s.message_id = published_sample.id

        with pecan.request.pipeline_manager.publisher(
                context.get_admin_context()) as publisher:
            publisher(published_samples)

        # TODO(asalkeld) this is not ideal, it would be nice if the publisher
        # returned the created sample message with message id (or at least the
        # a list of message_ids).
        return samples

    @wsme_pecan.wsexpose([Statistics], [Query], [unicode], int)
    def statistics(self, q=[], groupby=[], period=None):
        """Computes the statistics of the samples in the time range given.

        :param q: Filter rules for the data to be returned.
        :param groupby: Fields for group by aggregation
        :param period: Returned result will be an array of statistics for a
                       period long of that number of seconds.
        """
        if period and period < 0:
            error = _("Period must be positive.")
            pecan.response.translatable_error = error
            raise wsme.exc.ClientSideError(error)

        kwargs = _query_to_kwargs(q, storage.SampleFilter.__init__)
        kwargs['meter'] = self._id
        f = storage.SampleFilter(**kwargs)
        g = _validate_groupby_fields(groupby)
        computed = pecan.request.storage_conn.get_meter_statistics(f,
                                                                   period,
                                                                   g)
        LOG.debug('computed value coming from %r', pecan.request.storage_conn)
        # Find the original timestamp in the query to use for clamping
        # the duration returned in the statistics.
        start = end = None
        for i in q:
            if i.field == 'timestamp' and i.op in ('lt', 'le'):
                end = timeutils.parse_isotime(i.value).replace(tzinfo=None)
            elif i.field == 'timestamp' and i.op in ('gt', 'ge'):
                start = timeutils.parse_isotime(i.value).replace(tzinfo=None)

        return [Statistics(start_timestamp=start,
                           end_timestamp=end,
                           **c.as_dict())
                for c in computed]


class Meter(_Base):
    """One category of measurements.
    """

    name = wtypes.text
    "The unique name for the meter"

    type = wtypes.Enum(str, *sample.TYPES)
    "The meter type (see :ref:`measurements`)"

    unit = wtypes.text
    "The unit of measure"

    resource_id = wtypes.text
    "The ID of the :class:`Resource` for which the measurements are taken"

    project_id = wtypes.text
    "The ID of the project or tenant that owns the resource"

    user_id = wtypes.text
    "The ID of the user who last triggered an update to the resource"

    meter_id = wtypes.text
    "The unique identifier for the meter"

    def __init__(self, **kwargs):
        meter_id = base64.encodestring('%s+%s' % (kwargs['resource_id'],
                                                  kwargs['name']))
        kwargs['meter_id'] = meter_id
        super(Meter, self).__init__(**kwargs)

    @classmethod
    def sample(cls):
        return cls(name='instance',
                   type='gauge',
                   unit='instance',
                   resource_id='bd9431c1-8d69-4ad3-803a-8d4a6b89fd36',
                   project_id='35b17138-b364-4e6a-a131-8f3099c5be68',
                   user_id='efd87807-12d2-4b38-9c70-5f5c2ac427ff',
                   )


class MetersController(rest.RestController):
    """Works on meters."""

    @pecan.expose()
    def _lookup(self, meter_id, *remainder):
        # NOTE(gordc): drop last path if empty (Bug #1202739)
        if remainder and not remainder[-1]:
            remainder = remainder[:-1]
        return MeterController(meter_id), remainder

    @wsme_pecan.wsexpose([Meter], [Query])
    def get_all(self, q=[]):
        """Return all known meters, based on the data recorded so far.

        :param q: Filter rules for the meters to be returned.
        """
        kwargs = _query_to_kwargs(q, pecan.request.storage_conn.get_meters)
        return [Meter.from_db_model(m)
                for m in pecan.request.storage_conn.get_meters(**kwargs)]


class Resource(_Base):
    """An externally defined object for which samples have been received.
    """

    resource_id = wtypes.text
    "The unique identifier for the resource"

    project_id = wtypes.text
    "The ID of the owning project or tenant"

    user_id = wtypes.text
    "The ID of the user who created the resource or updated it last"

    timestamp = datetime.datetime
    "UTC date and time of the last update to any meter for the resource"

    metadata = {wtypes.text: wtypes.text}
    "Arbitrary metadata associated with the resource"

    links = [Link]
    "A list containing a self link and associated meter links"

    def __init__(self, metadata={}, **kwds):
        metadata = _flatten_metadata(metadata)
        super(Resource, self).__init__(metadata=metadata, **kwds)

    @classmethod
    def sample(cls):
        return cls(resource_id='bd9431c1-8d69-4ad3-803a-8d4a6b89fd36',
                   project_id='35b17138-b364-4e6a-a131-8f3099c5be68',
                   user_id='efd87807-12d2-4b38-9c70-5f5c2ac427ff',
                   timestamp=datetime.datetime.utcnow(),
                   metadata={'name1': 'value1',
                             'name2': 'value2'},
                   links=[Link(href=('http://localhost:8777/v2/resources/'
                                     'bd9431c1-8d69-4ad3-803a-8d4a6b89fd36'),
                               rel='self'),
                          Link(href=('http://localhost:8777/v2/meters/volume?'
                                     'q.field=resource_id&'
                                     'q.value=bd9431c1-8d69-4ad3-803a-'
                                     '8d4a6b89fd36'),
                               rel='volume')],
                   )


class ResourcesController(rest.RestController):
    """Works on resources."""

    def _resource_links(self, resource_id):
        links = [_make_link('self', pecan.request.host_url, 'resources',
                            resource_id)]
        for meter in pecan.request.storage_conn.get_meters(resource=
                                                           resource_id):
            query = {'field': 'resource_id', 'value': resource_id}
            links.append(_make_link(meter.name, pecan.request.host_url,
                                    'meters', meter.name, query=query))
        return links

    @wsme_pecan.wsexpose(Resource, unicode)
    def get_one(self, resource_id):
        """Retrieve details about one resource.

        :param resource_id: The UUID of the resource.
        """
        authorized_project = acl.get_limited_to_project(pecan.request.headers)
        resources = list(pecan.request.storage_conn.get_resources(
            resource=resource_id, project=authorized_project))
        # FIXME (flwang): Need to change this to return a 404 error code when
        # we get a release of WSME that supports it.
        if not resources:
            error = _("Unknown resource")
            pecan.response.translatable_error = error
            raise wsme.exc.InvalidInput("resource_id",
                                        resource_id,
                                        error)
        return Resource.from_db_and_links(resources[0],
                                          self._resource_links(resource_id))

    @wsme_pecan.wsexpose([Resource], [Query])
    def get_all(self, q=[]):
        """Retrieve definitions of all of the resources.

        :param q: Filter rules for the resources to be returned.
        """
        kwargs = _query_to_kwargs(q, pecan.request.storage_conn.get_resources)
        resources = [
            Resource.from_db_and_links(r,
                                       self._resource_links(r.resource_id))
            for r in pecan.request.storage_conn.get_resources(**kwargs)]
        return resources


class Alarm(_Base):
    """Representation of an alarm.
    """

    alarm_id = wtypes.text
    "The UUID of the alarm"

    name = wtypes.text
    "The name for the alarm"

    description = wtypes.text
    "The description of the alarm"

    meter_name = wtypes.text
    "The name of meter"

    project_id = wtypes.text
    "The ID of the project or tenant that owns the alarm"

    user_id = wtypes.text
    "The ID of the user who created the alarm"

    comparison_operator = wtypes.Enum(str, 'lt', 'le', 'eq', 'ne', 'ge', 'gt')
    "The comparison against the alarm threshold"

    threshold = float
    "The threshold of the alarm"

    statistic = wtypes.Enum(str, 'max', 'min', 'avg', 'sum', 'count')
    "The statistic to compare to the threshold"

    enabled = bool
    "This alarm is enabled?"

    evaluation_periods = int
    "The number of periods to evaluate the threshold"

    period = int
    "The time range in seconds over which to evaluate the threshold"

    timestamp = datetime.datetime
    "The date of the last alarm definition update"

    state = wtypes.Enum(str, 'ok', 'alarm', 'insufficient data')
    "The state offset the alarm"

    state_timestamp = datetime.datetime
    "The date of the last alarm state changed"

    ok_actions = [wtypes.text]
    "The actions to do when alarm state change to ok"

    alarm_actions = [wtypes.text]
    "The actions to do when alarm state change to alarm"

    insufficient_data_actions = [wtypes.text]
    "The actions to do when alarm state change to insufficient data"

    repeat_actions = bool
    "The actions should be re-triggered on each evaluation cycle"

    matching_metadata = {wtypes.text: wtypes.text}
    "The matching_metadata of the alarm"

    def __init__(self, **kwargs):
        super(Alarm, self).__init__(**kwargs)

    @classmethod
    def sample(cls):
        return cls(alarm_id=None,
                   name="SwiftObjectAlarm",
                   description="An alarm",
                   meter_name="storage.objects",
                   comparison_operator="gt",
                   threshold=200,
                   statistic="avg",
                   user_id="c96c887c216949acbdfbd8b494863567",
                   project_id="c96c887c216949acbdfbd8b494863567",
                   evaluation_periods=2,
                   period=240,
                   enabled=True,
                   timestamp=datetime.datetime.utcnow(),
                   state="ok",
                   state_timestamp=datetime.datetime.utcnow(),
                   ok_actions=["http://site:8000/ok"],
                   alarm_actions=["http://site:8000/alarm"],
                   insufficient_data_actions=["http://site:8000/nodata"],
                   matching_metadata={"key_name":
                                      "key_value"},
                   repeat_actions=False,
                   )


class AlarmChange(_Base):
    """Representation of an event in an alarm's history
    """

    event_id = wtypes.text
    "The UUID of the change event"

    alarm_id = wtypes.text
    "The UUID of the alarm"

    type = wtypes.Enum(str,
                       'creation',
                       'rule change',
                       'state transition',
                       'deletion')
    "The type of change"

    detail = wtypes.text
    "JSON fragment describing change"

    project_id = wtypes.text
    "The project ID of the initiating identity"

    user_id = wtypes.text
    "The user ID of the initiating identity"

    on_behalf_of = wtypes.text
    "The tenant on behalf of which the change is being made"

    timestamp = datetime.datetime
    "The time/date of the alarm change"

    @classmethod
    def sample(cls):
        return cls(alarm_id='e8ff32f772a44a478182c3fe1f7cad6a',
                   type='rule change',
                   detail='{"threshold": 42.0, "evaluation_periods": 4}',
                   user_id="3e5d11fda79448ac99ccefb20be187ca",
                   project_id="b6f16144010811e387e4de429e99ee8c",
                   on_behalf_of="92159030020611e3b26dde429e99ee8c",
                   timestamp=datetime.datetime.utcnow(),
                   )


class AlarmController(rest.RestController):
    """Manages operations on a single alarm.
    """

    _custom_actions = {
        'history': ['GET'],
    }

    def __init__(self, alarm_id):
        pecan.request.context['alarm_id'] = alarm_id
        self._id = alarm_id

    def _alarm(self):
        self.conn = pecan.request.storage_conn
        auth_project = acl.get_limited_to_project(pecan.request.headers)
        alarms = list(self.conn.get_alarms(alarm_id=self._id,
                                           project=auth_project))
        # FIXME (flwang): Need to change this to return a 404 error code when
        # we get a release of WSME that supports it.
        if len(alarms) < 1:
            error = _("Unknown alarm")
            pecan.response.translatable_error = error
            raise wsme.exc.ClientSideError(error)
        return alarms[0]

    def _record_change(self, data, now, on_behalf_of=None, type=None):
        if not cfg.CONF.alarm.record_history:
            return
        type = type or (storage.models.AlarmChange.STATE_TRANSITION
                        if data.get('state')
                        else storage.models.AlarmChange.RULE_CHANGE)
        detail = json.dumps(utils.stringify_timestamps(data))
        user_id = pecan.request.headers.get('X-User-Id')
        project_id = pecan.request.headers.get('X-Project-Id')
        on_behalf_of = on_behalf_of or project_id
        try:
            self.conn.record_alarm_change(dict(event_id=str(uuid.uuid4()),
                                               alarm_id=self._id,
                                               type=type,
                                               detail=detail,
                                               user_id=user_id,
                                               project_id=project_id,
                                               on_behalf_of=on_behalf_of,
                                               timestamp=now))
        except NotImplementedError:
            pass

    @wsme_pecan.wsexpose(Alarm, wtypes.text)
    def get(self):
        """Return this alarm."""
        return Alarm.from_db_model(self._alarm())

    @wsme.validate(Alarm)
    @wsme_pecan.wsexpose(Alarm, wtypes.text, body=Alarm)
    def put(self, data):
        """Modify this alarm."""
        # merge the new values from kwargs into the current
        # alarm "alarm_in".
        alarm_in = self._alarm()
        now = timeutils.utcnow()
        change = data.as_dict(storage.models.Alarm)
        data.state_timestamp = wsme.Unset
        data.alarm_id = self._id
        kwargs = data.as_dict(storage.models.Alarm)
        for k, v in kwargs.iteritems():
            setattr(alarm_in, k, v)
            if k == 'state':
                alarm_in.state_timestamp = now

        alarm = self.conn.update_alarm(alarm_in)
        self._record_change(change, now, on_behalf_of=alarm.project_id)
        return Alarm.from_db_model(alarm)

    @wsme_pecan.wsexpose(None, wtypes.text, status_code=204)
    def delete(self):
        """Delete this alarm."""
        # ensure alarm exists before deleting
        alarm = self._alarm()
        self.conn.delete_alarm(alarm.alarm_id)
        change = Alarm.from_db_model(alarm).as_dict(storage.models.Alarm)
        self._record_change(change,
                            timeutils.utcnow(),
                            type=storage.models.AlarmChange.DELETION)

    # TODO(eglynn): add pagination marker to signature once overall
    #               API support for pagination is finalized
    @wsme_pecan.wsexpose([AlarmChange], [Query])
    def history(self, q=[]):
        """Assembles the alarm history requested.

        :param q: Filter rules for the changes to be described.
        """
        # allow history to be returned for deleted alarms, but scope changes
        # returned to those carried out on behalf of the auth'd tenant, to
        # avoid inappropriate cross-tenant visibility of alarm history
        auth_project = acl.get_limited_to_project(pecan.request.headers)
        conn = pecan.request.storage_conn
        kwargs = _query_to_kwargs(q, conn.get_alarm_changes, ['on_behalf_of'])
        return [AlarmChange.from_db_model(ac)
                for ac in conn.get_alarm_changes(self._id, auth_project,
                                                 **kwargs)]


class AlarmsController(rest.RestController):
    """Manages operations on the alarms collection.
    """

    @pecan.expose()
    def _lookup(self, alarm_id, *remainder):
        if remainder and not remainder[-1]:
            remainder = remainder[:-1]
        return AlarmController(alarm_id), remainder

    def _record_creation(self, conn, data, alarm_id, now):
        if not cfg.CONF.alarm.record_history:
            return
        type = storage.models.AlarmChange.CREATION
        detail = json.dumps(utils.stringify_timestamps(data))
        user_id = pecan.request.headers.get('X-User-Id')
        project_id = pecan.request.headers.get('X-Project-Id')
        try:
            conn.record_alarm_change(dict(event_id=str(uuid.uuid4()),
                                          alarm_id=alarm_id,
                                          type=type,
                                          detail=detail,
                                          user_id=user_id,
                                          project_id=project_id,
                                          on_behalf_of=project_id,
                                          timestamp=now))
        except NotImplementedError:
            pass

    @wsme.validate(Alarm)
    @wsme_pecan.wsexpose(Alarm, body=Alarm, status_code=201)
    def post(self, data):
        """Create a new alarm."""
        conn = pecan.request.storage_conn

        now = timeutils.utcnow()
        data.alarm_id = str(uuid.uuid4())
        data.user_id = pecan.request.headers.get('X-User-Id')
        data.project_id = pecan.request.headers.get('X-Project-Id')
        data.state_timestamp = wsme.Unset
        change = data.as_dict(storage.models.Alarm)
        data.timestamp = now

        # make sure alarms are unique by name per project.
        alarms = list(conn.get_alarms(name=data.name,
                                      project=data.project_id))
        if len(alarms) > 0:
            error = _("Alarm with that name exists")
            pecan.response.translatable_error = error
            raise wsme.exc.ClientSideError(error)

        try:
            kwargs = data.as_dict(storage.models.Alarm)
            alarm_in = storage.models.Alarm(**kwargs)
        except Exception as ex:
            LOG.exception(ex)
            error = _("Alarm incorrect")
            pecan.response.translatable_error = error
            raise wsme.exc.ClientSideError(error)

        alarm = conn.create_alarm(alarm_in)
        self._record_creation(conn, change, alarm.alarm_id, now)
        return Alarm.from_db_model(alarm)

    @wsme_pecan.wsexpose([Alarm], [Query])
    def get_all(self, q=[]):
        """Return all alarms, based on the query provided.

        :param q: Filter rules for the alarms to be returned.
        """
        kwargs = _query_to_kwargs(q,
                                  pecan.request.storage_conn.get_alarms)
        return [Alarm.from_db_model(m)
                for m in pecan.request.storage_conn.get_alarms(**kwargs)]


class V2Controller(object):
    """Version 2 API controller root."""

    resources = ResourcesController()
    meters = MetersController()
    alarms = AlarmsController()
