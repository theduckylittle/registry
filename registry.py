import datetime
import isodate
import json
import os
import rawes
import re
import requests
import sys
import getopt
import yaml
import io
import logging

from dateutil import tz
from dateutil.parser import parse

from distutils.util import strtobool

from django.conf import settings
from django.core import management
from django.conf.urls import url
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from pycsw import server
from pycsw.core import config, metadata
from pycsw.core import admin as pycsw_admin
from pycsw.core.etree import etree
from pycsw.core.repository import Repository
from pycsw.core.util import wkt2geom

from mapproxy.config.config import load_default_config, load_config
from mapproxy.config.spec import validate_options
from mapproxy.config.validator import validate_references
from mapproxy.config.loader import ProxyConfiguration, ConfigurationError
from mapproxy.wsgiapp import MapProxyApp

from shapely.geometry import box

from six.moves.urllib_parse import urlparse, unquote as url_unquote

from rawes.elastic_exception import ElasticException

LOGGER = logging.getLogger(__name__)

__version__ = 0.1

DEBUG = strtobool(os.getenv('REGISTRY_DEBUG', 'True'))
ROOT_URLCONF = 'registry'
DATABASES = {'default': {}}  # required regardless of actual usage
SECRET_KEY = os.getenv('REGISTRY_SECRET_KEY', 'Make sure you create a good secret key.')

REGISTRY_MAPPING_PRECISION = os.getenv('REGISTRY_MAPPING_PRECISION', '500m')
REGISTRY_SEARCH_URL = os.getenv('REGISTRY_SEARCH_URL', 'http://127.0.0.1:9200')
REGISTRY_DATABASE_URL = os.getenv('REGISTRY_DATABASE_URL', 'sqlite:////tmp/registry.db')

VCAP_SERVICES = os.environ.get('VCAP_SERVICES', None)


def vcaps_search_url(VCAP_SERVICES, registry_url):
    """Extract registry_url from VCAP_SERVICES dict
    """
    if VCAP_SERVICES:
        vcap_config = json.loads(VCAP_SERVICES)
        if 'searchly' in vcap_config:
            registry_url = vcap_config['searchly'][0]['credentials']['sslUri']

    return registry_url


# Override REGISTRY_SEARCH_URL if VCAP_SERVICES is defined.
REGISTRY_SEARCH_URL = vcaps_search_url(VCAP_SERVICES, REGISTRY_SEARCH_URL)

TIMEZONE = tz.gettz('America/New_York')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': True,
        },
        'pycsw': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': True,
        },
        'mapproxy': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': True,
        },
        'registry': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': True,
        },
    },
}
if not settings.configured:
    settings.configure(**locals())

# When importing serializers, Django requires DEFAULT_INDEX_TABLESPACE.
# This variable is set after settings.configure().
from rest_framework import serializers # noqa

PYCSW = {
    'repository': {
        'source': 'registry.RegistryRepository',
        'mappings': 'registry',
        'database': REGISTRY_DATABASE_URL,
        'table': 'records',
    },
    'server': {
        'maxrecords': '100',
        'pretty_print': 'true',
        'domaincounts': 'true',
        'encoding': 'UTF-8',
        'profiles': 'apiso',
        'home': '.',
    },
    'metadata:main': {
        'identification_title': 'Registry',
        'identification_abstract': 'Registry is a CSW catalogue with faceting capabilities via OpenSearch',
        'identification_keywords': 'registry, pycsw',
        'identification_keywords_type': 'theme',
        'identification_fees': 'None',
        'identification_accessconstraints': 'None',
        'provider_name': 'Organization Name',
        'provider_url': '',
        'contact_name': 'Lastname, Firstname',
        'contact_position': 'Position Title',
        'contact_address': 'Mailing Address',
        'contact_city': 'City',
        'contact_stateorprovince': 'Administrative Area',
        'contact_postalcode': 'Zip or Postal Code',
        'contact_country': 'Country',
        'contact_phone': '+xx-xxx-xxx-xxxx',
        'contact_fax': '+xx-xxx-xxx-xxxx',
        'contact_email': 'Email Address',
        'contact_url': 'Contact URL',
        'contact_hours': 'Hours of Service',
        'contact_instructions': 'During hours of service. Off on weekends.',
        'contact_role': 'pointOfContact',
    },
    'manager': {
        'transactions': 'false',
        'allowed_ips': os.getenv('REGISTRY_ALLOWED_IPS', '*'),
    },
}

MD_CORE_MODEL = {
    'typename': 'pycsw:CoreMetadata',
    'outputschema': 'http://pycsw.org/metadata',
    'mappings': {
        'pycsw:Identifier': 'identifier',
        'pycsw:Typename': 'typename',
        'pycsw:Schema': 'schema',
        'pycsw:MdSource': 'mdsource',
        'pycsw:InsertDate': 'insert_date',
        'pycsw:XML': 'xml',
        'pycsw:AnyText': 'anytext',
        'pycsw:Language': 'language',
        'pycsw:Title': 'title',
        'pycsw:Abstract': 'abstract',
        'pycsw:Keywords': 'keywords',
        'pycsw:KeywordType': 'keywordstype',
        'pycsw:Format': 'format',
        'pycsw:Source': 'source',
        'pycsw:Date': 'date',
        'pycsw:Modified': 'date_modified',
        'pycsw:Type': 'type',
        'pycsw:BoundingBox': 'wkt_geometry',
        'pycsw:CRS': 'crs',
        'pycsw:AlternateTitle': 'title_alternate',
        'pycsw:RevisionDate': 'date_revision',
        'pycsw:CreationDate': 'date_creation',
        'pycsw:PublicationDate': 'date_publication',
        'pycsw:OrganizationName': 'organization',
        'pycsw:SecurityConstraints': 'securityconstraints',
        'pycsw:ParentIdentifier': 'parentidentifier',
        'pycsw:TopicCategory': 'topicategory',
        'pycsw:ResourceLanguage': 'resourcelanguage',
        'pycsw:GeographicDescriptionCode': 'geodescode',
        'pycsw:Denominator': 'denominator',
        'pycsw:DistanceValue': 'distancevalue',
        'pycsw:DistanceUOM': 'distanceuom',
        'pycsw:TempExtent_begin': 'time_begin',
        'pycsw:TempExtent_end': 'time_end',
        'pycsw:ServiceType': 'servicetype',
        'pycsw:ServiceTypeVersion': 'servicetypeversion',
        'pycsw:Operation': 'operation',
        'pycsw:CouplingType': 'couplingtype',
        'pycsw:OperatesOn': 'operateson',
        'pycsw:OperatesOnIdentifier': 'operatesonidentifier',
        'pycsw:OperatesOnName': 'operatesoname',
        'pycsw:Degree': 'degree',
        'pycsw:AccessConstraints': 'accessconstraints',
        'pycsw:OtherConstraints': 'otherconstraints',
        'pycsw:Classification': 'classification',
        'pycsw:ConditionApplyingToAccessAndUse': 'conditionapplyingtoaccessanduse',
        'pycsw:Lineage': 'lineage',
        'pycsw:ResponsiblePartyRole': 'responsiblepartyrole',
        'pycsw:SpecificationTitle': 'specificationtitle',
        'pycsw:SpecificationDate': 'specificationdate',
        'pycsw:SpecificationDateType': 'specificationdatetype',
        'pycsw:Creator': 'creator',
        'pycsw:Publisher': 'publisher',
        'pycsw:Contributor': 'contributor',
        'pycsw:Relation': 'relation',
        'pycsw:Links': 'links',
    }
}


# TODO: make registry work using CSRF cookie.
@method_decorator(csrf_exempt, name='dispatch')
def csw_view(request, catalog=None):
    """CSW dispatch view.
       Wraps the WSGI call and allows us to tweak any django settings.
    """

    # PUT creates catalog, DELETE removes catalog.
    # GET and POST are managed by pycsw.

    if catalog and request.META['REQUEST_METHOD'] == 'PUT':
        message = create_index(catalog)
        return HttpResponse(message, status=200)

    if catalog and request.META['REQUEST_METHOD'] == 'DELETE':
        message, status = delete_index(catalog)
        return HttpResponse(message, status=status)

    env = request.META.copy()
    env.update({'local.app_root': os.path.dirname(__file__),
                'REQUEST_URI': request.build_absolute_uri()})

    # pycsw prefers absolute urls, let's get them from the request.
    url = request.build_absolute_uri()
    PYCSW['server']['url'] = url
    PYCSW['metadata:main']['provider_url'] = url

    # Enable CSW-T when a catalog is defined in the
    if catalog:
        PYCSW['manager']['transactions'] = 'true'

    csw = server.Csw(PYCSW, env)
    status, content = csw.dispatch_wsgi()
    status_code = int(status[0:3])
    response = HttpResponse(content,
                            content_type=csw.contenttype,
                            status=status_code,
                            )

    return response


def delete_index(catalog, es=None):
    if not es:
        es, version = es_connect(url=REGISTRY_SEARCH_URL)

    try:
        es.delete(catalog)
        message, status = 'Catalog {0} removed succesfully'.format(catalog), 200
    except ElasticException:
        message, status = 'Catalog does not exist!', 404

    return message, status


def record_to_dict(record):
    # Get bounding box from wkt geometry if it exists in the record.
    bbox = (-180, -90, 180, 90)
    if record.wkt_geometry:
        bbox = wkt2geom(record.wkt_geometry)
    min_x, min_y, max_x, max_y = bbox[0], bbox[1], bbox[2], bbox[3]
    record_dict = {
        'title': record.title.encode('ascii', 'ignore').decode('utf-8'),
        'abstract': record.abstract,
        'bbox': bbox,
        'min_x': min_x,
        'min_y': min_y,
        'max_x': max_x,
        'max_y': max_y,
        'layer_date': record.date_modified,
        'layer_originator': record.creator,
        'layer_identifier': record.identifier,
        # 'rectangle': box(min_x, min_y, max_x, max_y),
        'layer_geoshape': {
            'type': 'envelope',
            'coordinates': [
                [min_x, max_y], [max_x, min_y]
            ]
        }
    }

    return record_dict


def check_index_exists(catalog, es=None):
    if es is None:
        es, version = es_connect(url=REGISTRY_SEARCH_URL)

    result = False
    indices = es.get('_aliases').keys()
    if catalog in indices:
        result = True

    return result


def create_index(catalog, es=None, version=None):
    if es is None:
        es, version = es_connect(url=REGISTRY_SEARCH_URL)

    mapping = es_mapping(version)
    es.put(catalog, data=mapping)

    return 'Catalog {0} created succesfully'.format(catalog)


def es_connect(url):
    es = rawes.Elastic(url)
    version = es.get('')['version']['number']

    return es, version


def es_mapping(version):
    return {
        "mappings": {
            "layer": {
                "properties": {
                    "layer_geoshape": {
                        "type": "geo_shape",
                        "tree": "quadtree",
                        "precision": REGISTRY_MAPPING_PRECISION
                    },
                    "title": text_field(version, copy_to="alltext"),
                    "abstract": text_field(version, copy_to="alltext"),
                    "alltext": text_field(version)
                }
            }
        }
    }


def text_field(version, **kwargs):
    field_def = {"type": "string", "index": "analyzed"}
    if version == '5.0.0':
        field_def = {"type": "text"}
    field_def.update(kwargs)
    return field_def


def parse_catalog_from_url(url):
    parsed_url = urlparse(url)
    catalog_slug = parsed_url.path.split('/')[2]

    return catalog_slug


class RegistryRepository(Repository):
    def __init__(self, *args, **kwargs):
        if args and hasattr(args[0], 'url'):
            url = args[0].url
            self.catalog = parse_catalog_from_url(url) if urlparse(url).path != '/csw' else None

        try:
            self.es, self.version = es_connect(url=REGISTRY_SEARCH_URL)
            self.es_status = 200
        except requests.exceptions.ConnectionError:
            self.es_status = 404

        database = PYCSW['repository']['database']

        return super(RegistryRepository, self).__init__(database, context=config.StaticContext())

    def insert(self, *args, **kwargs):
        record = args[0]
        super(RegistryRepository, self).insert(*args)
        if self.es_status != 200:
            return
        if not check_index_exists(self.catalog):
            print('Cannot add layer {0}. Catalog {1} does not exist!'.format(record.identifier, self.catalog))
            return

        es_dict = record_to_dict(record)
        # TODO: Do not index wrong bounding boxes.
        try:
            self.es[self.catalog]['layer'].post(data=es_dict)
            print("Record {0} indexed".format(es_dict['title']))
        except ElasticException as e:
            print(e)


def parse_get_params(request):
    """
    parse all url get params that contains dots in a representation of
    serializer field names, for example: d.docs.limit to d_docs_limit.
    that makes compatible an actual API client with django-rest-framework
    serializers.
    :param request:
    :return: QueryDict with parsed get params.
    """

    get = request.GET.copy()
    new_get = request.GET.copy()
    for key in get.keys():
        if key.count(".") > 0:
            new_key = key.replace(".", "_")
            new_get[new_key] = get.get(key)
            del new_get[key]

    return new_get


def parse_datetime_range_to_solr(time_filter):
    start, end = parse_datetime_range(time_filter)
    left = "*"
    right = "*"

    if start.get("parsed_datetime"):
        left = start.get("parsed_datetime")
        if start.get("is_common_era"):
            left = start.get("parsed_datetime").isoformat().replace("+00:00", "") + 'Z'

    if end.get("parsed_datetime"):
        right = end.get("parsed_datetime")
        if end.get("is_common_era"):
            right = end.get("parsed_datetime").isoformat().replace("+00:00", "") + 'Z'

    return "[{0} TO {1}]".format(left, right)


def parse_geo_box(geo_box_str):
    """
    parses [-90,-180 TO 90,180] to a shapely.geometry.box
    :param geo_box_str:
    :return:
    """

    from_point_str, to_point_str = parse_solr_geo_range_as_pair(geo_box_str)
    from_point = parse_lat_lon(from_point_str)
    to_point = parse_lat_lon(to_point_str)
    rectangle = box(from_point[0], from_point[1], to_point[0], to_point[1])
    return rectangle


def parse_datetime_range(time_filter):
    """
    Parse the url param to python objects.
    From what time range to divide by a.time.gap into intervals.
    Defaults to q.time and otherwise 90 days.
    Validate in API: re.search("\\[(.*) TO (.*)\\]", value)
    :param time_filter: [2013-03-01 TO 2013-05-01T00:00:00]
    :return: datetime.datetime(2013, 3, 1, 0, 0), datetime.datetime(2013, 5, 1, 0, 0)
    """

    start, end = parse_solr_time_range_as_pair(time_filter)
    start, end = parse_datetime(start), parse_datetime(end)
    return start, end


def parse_solr_time_range_as_pair(time_filter):
    """
    :param time_filter: [2013-03-01 TO 2013-05-01T00:00:00]
    :return: (2013-03-01, 2013-05-01T00:00:00)
    """
    pattern = "\\[(.*) TO (.*)\\]"
    matcher = re.search(pattern, time_filter)
    if matcher:
        return matcher.group(1), matcher.group(2)
    else:
        raise Exception("Regex {0} couldn't parse {1}".format(pattern, time_filter))


def parse_solr_geo_range_as_pair(geo_box_str):
    """
    :param geo_box_str: [-90,-180 TO 90,180]
    :return: ("-90,-180", "90,180")
    """
    pattern = "\\[(.*) TO (.*)\\]"
    matcher = re.search(pattern, geo_box_str)
    if matcher:
        return matcher.group(1), matcher.group(2)
    else:
        raise Exception("Regex {0} could not parse {1}".format(pattern, geo_box_str))


def parse_lat_lon(point_str):
    lat, lon = map(float, point_str.split(','))
    return lat, lon


def parse_datetime(date_str):
    """
    Parses a date string to date object.
    for BCE dates, only supports the year part.
    """
    is_common_era = True
    date_str_parts = date_str.split("-")
    if date_str_parts and date_str_parts[0] == '':
        is_common_era = False
        # for now, only support BCE years

        # assume the datetime comes complete, but
        # when it comes only the year, add the missing datetime info:
        if len(date_str_parts) == 2:
            date_str = date_str + "-01-01T00:00:00Z"

    parsed_datetime = {
        'is_common_era': is_common_era,
        'parsed_datetime': None
    }

    if is_common_era:
        if date_str == '*':
            return parsed_datetime  # open ended.

        default = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0,
            day=1, month=1
        )
        parsed_datetime['parsed_datetime'] = parse(date_str, default=default)
        return parsed_datetime

    parsed_datetime['parsed_datetime'] = date_str
    return parsed_datetime


def gap_to_elastic(time_gap):
    # elastic units link: https://www.elastic.co/guide/en/elasticsearch/reference/current/common-options.html#time-units
    elastic_units = {
        "YEARS": 'y',
        "MONTHS": 'm',
        "WEEKS": 'w',
        "DAYS": 'd',
        "HOURS": 'h',
        "MINUTES": 'm',
        "SECONDS": 's'
    }
    quantity, unit = parse_ISO8601(time_gap)
    interval = "{0}{1}".format(str(quantity), elastic_units[unit[0]])

    return interval


def parse_ISO8601(time_gap):
    """
    P1D to (1, ("DAYS", isodate.Duration(days=1)).
    P1Y to (1, ("YEARS", isodate.Duration(years=1)).
    :param time_gap: ISO8601 string.
    :return: tuple with quantity and unit of time.
    """
    matcher = None

    if time_gap.count("T"):
        units = {
            "H": ("HOURS", isodate.Duration(hours=1)),
            "M": ("MINUTES", isodate.Duration(minutes=1)),
            "S": ("SECONDS", isodate.Duration(seconds=1))
        }
        matcher = re.search("PT(\d+)([HMS])", time_gap)
        if matcher:
            quantity = int(matcher.group(1))
            unit = matcher.group(2)
            return quantity, units.get(unit)
        else:
            raise Exception("Does not match the pattern: {}".format(time_gap))
    else:
        units = {
            "Y": ("YEARS", isodate.Duration(years=1)),
            "M": ("MONTHS", isodate.Duration(months=1)),
            "W": ("WEEKS", isodate.Duration(weeks=1)),
            "D": ("DAYS", isodate.Duration(days=1))
        }
        matcher = re.search("P(\d+)([YMWD])", time_gap)
        if matcher:
            quantity = int(matcher.group(1))
            unit = matcher.group(2)
        else:
            raise Exception("Does not match the pattern: {}".format(time_gap))

    return quantity, units.get(unit)


class SearchSerializer(serializers.Serializer):
    q_time = serializers.CharField(
        required=False,
        help_text="Constrains docs by time range. Either side can be '*' to signify open-ended. "
                  "Otherwise it must be in either format as given in the example. UTC time zone is implied. Example: "
                  "[2013-03-01 TO 2013-04-01T00:00:00]",
        # default="[1900-01-01 TO 2016-12-31T00:00:00]"
    )
    search_engine_endpoint = serializers.CharField(
        required=False,
        help_text="Endpoint URL",
        default=REGISTRY_SEARCH_URL
    )
    q_uuid = serializers.CharField(
        required=False,
        help_text="Layer uuid"
    )
    q_geo = serializers.CharField(
        required=False,
        help_text="A rectangular geospatial filter in decimal degrees going from the lower-left to the upper-right. "
                  "The coordinates are in lat,lon format. "
                  "Example: [-90,-180 TO 90,180]",
        default="[-90,-180 TO 90,180]"
    )
    q_text = serializers.CharField(
        required=False,
        help_text="Constrains docs by keyword search query."
    )
    q_user = serializers.CharField(
        required=False,
        help_text="Constrains docs by matching exactly a certain user."
    )
    d_docs_limit = serializers.IntegerField(
        required=False,
        help_text="How many documents to return.",
        default=100
    )
    d_docs_page = serializers.IntegerField(
        required=False,
        help_text="When documents to return are more than d_docs_limit they can be paginated by this value.",
        default=1
    )
    d_docs_sort = serializers.ChoiceField(
        required=False,
        help_text="How to order the documents before returning the top X. 'score' is keyword search relevancy. "
                  "'time' is time descending. 'distance' is the distance between the doc and the middle of q.geo.",
        default="score",
        choices=["score", "time", "distance"]
    )
    a_time_limit = serializers.IntegerField(
        required=False,
        help_text="Non-0 triggers time/date range faceting. This value is the maximum number of time ranges to "
                  "return when a.time.gap is unspecified. This is a soft maximum; less will usually be returned. "
                  "A suggested value is 100. Note that a.time.gap effectively ignores this value. "
                  "See Solr docs for more details on the query/response format.",
        default=0
    )
    a_time_gap = serializers.CharField(
        required=False,
        help_text="The consecutive time interval/gap for each time range. Ignores a.time.limit.The format is based on "
                  "a subset of the ISO-8601 duration format."
    )
    a_hm_limit = serializers.IntegerField(
        required=False,
        help_text=("Non-0 triggers heatmap/grid faceting. "
                   "This number is a soft maximum on thenumber of cells it should have. "
                   "There may be as few as 1/4th this number in return. "
                   "Note that a.hm.gridLevel can effectively ignore this value. "
                   "The response heatmap contains a counts grid that can be null or contain null rows when "
                   "all its values would be 0. See Solr docs for more details on the response format."),
        default=0
    )
    a_hm_gridlevel = serializers.IntegerField(
        required=False,
        help_text="To explicitly specify the grid level, e.g. to let a user ask for greater or courser resolution "
                  "than the most recent request. Ignores a.hm.limit."
    )
    a_hm_filter = serializers.CharField(
        required=False,
        help_text="To explicitly specify the grid level, e.g. to let a user ask for greater or courser resolution "
                  "than the most recent request. Ignores a.hm.limit."
    )

    a_text_limit = serializers.IntegerField(
        required=False,
        help_text="Returns the most frequently occurring words. WARNING: There is usually a significant performance "
                  "hit in this due to the extremely high cardinality.",
        default=0
    )
    a_user_limit = serializers.IntegerField(
        required=False,
        help_text="Returns the most frequently occurring users.",
        default=0
    )
    original_response = serializers.IntegerField(
        required=False,
        help_text="Returns te original search engine response.",
        default=0
    )

    def validate_q_time(self, value):
        """
        Would be for example: [2013-03-01 TO 2013-04-01T00:00:00] and/or [* TO *]
        Returns a valid sorl value. [2013-03-01T00:00:00Z TO 2013-04-01T00:00:00Z] and/or [* TO *]
        """
        try:
            range = parse_datetime_range_to_solr(value)
            return range
        except Exception as e:
            raise serializers.ValidationError(e)

    def validate_q_geo(self, value):
        """
        Would be for example: [-90,-180 TO 90,180]
        """
        try:
            rectangle = parse_geo_box(value)
            return "[{0},{1} TO {2},{3}]".format(
                rectangle.bounds[0],
                rectangle.bounds[1],
                rectangle.bounds[2],
                rectangle.bounds[3],
            )
        except Exception as e:
            raise serializers.ValidationError(e)

    def validate_d_docs_page(self, value):
        """
        paginations cant be zero or negative.
        :param value:
        :return:
        """
        if value <= 0:
            raise serializers.ValidationError("d_docs_page cant be zero or negative")
        return value


def elasticsearch(serializer, catalog):
    """
    https://www.elastic.co/guide/en/elasticsearch/reference/current/_the_search_api.html
    :param serializer:
    :return:
    """

    search_endpoint = serializer.validated_data.get("search_engine_endpoint")
    search_engine_endpoint = "{0}/_search".format(search_endpoint)
    if catalog:
        search_engine_endpoint = "{0}/{1}/_search".format(search_endpoint, catalog)
    q_text = serializer.validated_data.get("q_text")
    q_time = serializer.validated_data.get("q_time")
    q_geo = serializer.validated_data.get("q_geo")
    q_user = serializer.validated_data.get("q_user")
    q_uuid = serializer.validated_data.get("q_uuid")
    d_docs_sort = serializer.validated_data.get("d_docs_sort")
    d_docs_limit = int(serializer.validated_data.get("d_docs_limit"))
    d_docs_page = int(serializer.validated_data.get("d_docs_page"))
    a_time_gap = serializer.validated_data.get("a_time_gap")
    a_time_limit = serializer.validated_data.get("a_time_limit")
    original_response = serializer.validated_data.get("original_response")

    # Dict for search on Elastic engine
    must_array = []
    filter_dic = {}
    aggs_dic = {}

    # get ES version to make the query builder to be backward compatible with
    # diffs versions.
    # TODO: move this to a proper place. maybe ES client?.
    # TODO: cache it to avoid overwhelm ES with this call.
    # TODO: ask for ES_VERSION when building queries with an elegant way.
    ES_VERSION = 2
    response = requests.get(REGISTRY_SEARCH_URL)
    if response.ok:
        # looks ugly but will work on normal ES response for "/".
        ES_VERSION = int(response.json()["version"]["number"][0])

    # String searching
    if q_text:
        # Wrapping query string into a query filter.
        query_string = {
            "query_string": {
                "query": q_text
            }
        }
        if ES_VERSION < 2:
            query_string = {
                "query": {
                    "query_string": {
                        "query": q_text
                    }
                }
            }

        # add string searching
        must_array.append(query_string)

    if q_uuid:
        # Wrapping query string into a query filter.
        query_string = {
            "query_string": {
                "query": q_uuid
            }
        }
        if ES_VERSION < 2:
            query_string = {
                "query": {
                    "query_string": {
                        "query": q_uuid
                    }
                }
            }

        # add string searching
        must_array.append(query_string)


    if q_time:
        # check if q_time exists
        q_time = str(q_time)  # check string
        shortener = q_time[1:-1]
        shortener = shortener.split(" TO ")
        gte = shortener[0]  # greater than
        lte = shortener[1]  # less than
        layer_date = {}
        if gte == '*' and lte != '*':
            layer_date["lte"] = lte
            range_time = {
                "layer_date": layer_date
            }
            range_time = {"range": range_time}
            must_array.append(range_time)
        if gte != '*' and lte == '*':
            layer_date["gte"] = gte
            range_time = {
                "layer_date": layer_date
            }
            range_time = {"range": range_time}
            must_array.append(range_time)
        if gte != '*' and lte != '*':
            layer_date["gte"] = gte
            layer_date["lte"] = lte
            range_time = {
                "layer_date": layer_date
            }
            range_time = {"range": range_time}
            must_array.append(range_time)
    # geo_shape searching
    if q_geo:
        q_geo = str(q_geo)
        q_geo = q_geo[1:-1]
        Ymin, Xmin = q_geo.split(" TO ")[0].split(",")
        Ymax, Xmax = q_geo.split(" TO ")[1].split(",")
        geoshape_query = {
            "layer_geoshape": {
                "shape": {
                    "type": "envelope",
                    "coordinates": [[Xmin, Ymax], [Xmax, Ymin]]
                },
                "relation": "intersects"
            }
        }
        filter_dic["geo_shape"] = geoshape_query

    if q_user:
        # Using q_user
        user_searching = {
            "term": {
                "layer_originator": q_user
            }
        }
        must_array.append(user_searching)

    dic_query = {
        "query": {
            "bool": {
                "must": must_array,
                "filter": filter_dic
            }
        }
    }

    if ES_VERSION < 2:
        dic_query = {
            "query": {
                "filtered": {
                    "filter": {
                        "bool": {
                            "must": must_array,
                            "should": filter_dic
                        }
                    }
                }
            }
        }

    # Page
    if d_docs_limit:
        dic_query["size"] = d_docs_limit

    if d_docs_page:
        dic_query["from"] = d_docs_limit * d_docs_page - d_docs_limit

    if d_docs_sort == "score":
        dic_query["sort"] = {"_score": {"order": "desc"}}

    if d_docs_sort == "time":
        dic_query["sort"] = {"layer_date": {"order": "desc"}}

    if a_time_limit:
        # TODO: Work in progress, a_time_limit is incomplete.
        # TODO: when times are * it does not work. also a a_time_gap is not required.
        if q_time:
            if not a_time_gap:
                msg = "If you want to use a_time_limit feature, a_time_gap MUST BE initialized"
                return 400, {"error": {"msg": msg}}
        else:
            msg = "If you want to use a_time_limit feature, q_time MUST BE initialized"
            return 400, {"error": {"msg": msg}}

    if a_time_gap:
        interval = gap_to_elastic(a_time_gap)
        time_gap = {
            "date_histogram": {
                "field": "layer_date",
                "format": "yyyy-MM-dd'T'HH:mm:ssZ",
                "interval": interval
            }
        }
        aggs_dic['articles_over_time'] = time_gap

    # adding aggreations on body query
    if aggs_dic:
        dic_query['aggs'] = aggs_dic
    try:
        res = requests.post(search_engine_endpoint, data=json.dumps(dic_query))
    except Exception as e:
        return 500, {"error": {"msg": str(e)}}

    es_response = res.json()

    if original_response:
        return es_response

    data = {}

    if 'error' in es_response:
        data["error"] = es_response["error"]
        return 400, data

    data["request_url"] = res.url
    data["request_body"] = json.dumps(dic_query)
    data["a.matchDocs"] = es_response['hits']['total']
    docs = []
    # aggreations response: facets searching
    if 'aggregations' in es_response:
        aggs = es_response['aggregations']
        if 'articles_over_time' in aggs:
            gap_count = []
            a_gap = {}
            gap_resp = aggs["articles_over_time"]["buckets"]

            start = "*"
            end = "*"

            if len(gap_resp) > 0:
                start = gap_resp[0]['key_as_string'].replace('+0000', 'z')
                end = gap_resp[-1]['key_as_string'].replace('+0000', 'z')

            a_gap['start'] = start
            a_gap['end'] = end
            a_gap['gap'] = a_time_gap

            for item in gap_resp:
                temp = {}
                if item['doc_count'] != 0:
                    temp['count'] = item['doc_count']
                    temp['value'] = item['key_as_string'].replace('+0000', 'z')
                    gap_count.append(temp)
            a_gap['counts'] = gap_count
            data['a.time'] = a_gap

    if not int(d_docs_limit) == 0:
        for item in es_response['hits']['hits']:
            # data
            temp = item['_source']['abstract']
            if temp:
                item['_source']['abstract'] = temp.encode('ascii', 'ignore').decode('utf-8')
            docs.append(item['_source'])

    data["d.docs"] = docs

    return data


def search_view(request, catalog=None):
    request.GET = parse_get_params(request)
    serializer = SearchSerializer(data=request.GET)
    try:
        serializer.is_valid(raise_exception=True)
        data = elasticsearch(serializer, catalog)
        data = json.dumps(data)
        status = 200
    except serializers.ValidationError as error:
        data = error
        status = 400

    return HttpResponse(data, status=status, content_type='application/json')


def configure_mapproxy(extra_config, seed=False, ignore_warnings=True, renderd=False):
    """Create an validate mapproxy configuration based on a dict.
    """
    # Start with a sane configuration using MapProxy's defaults
    conf_options = load_default_config()

    # Merge both
    load_config(conf_options, config_dict=extra_config)

    # Make sure the config is valid.
    errors, informal_only = validate_options(conf_options)
    for error in errors:
        LOGGER.warn(error)
    if errors and not ignore_warnings:
        raise ConfigurationError('invalid configuration: %s' % ', '.join(errors))

    errors = validate_references(conf_options)
    for error in errors:
        LOGGER.warn(error)
    if errors and not ignore_warnings:
        raise ConfigurationError('invalid references: %s' % ', '.join(errors))

    conf = ProxyConfiguration(conf_options, seed=seed, renderd=renderd)

    return conf


def get_mapproxy(layer, seed=False, ignore_warnings=True, renderd=False, config_as_yaml=True):
    """Creates a mapproxy config for a given layer-like object.
       Compatible with django-registry and GeoNode.
    """
    bbox = list(wkt2geom(layer.wkt_geometry))
    bbox = ",".join([format(x, '.4f') for x in bbox])
    url = str(layer.source)

    layer_name = str(layer.title)

    srs = 'EPSG:4326'
    bbox_srs = 'EPSG:4326'
    grid_srs = 'EPSG:3857'

    default_source = {
        'type': 'wms',
        'coverage': {
            'bbox': bbox,
            'srs': srs,
            'bbox_srs': bbox_srs,
            'supported_srs': ['EPSG:4326', 'EPSG:900913', 'EPSG:3857'],
        },
        'req': {
            'layers': str(layer.title),
            'url': url,
            'transparent': True,
        },
    }

    if layer.type == 'ESRI:ArcGIS:MapServer' or layer.type == 'ESRI:ArcGIS:ImageServer':
        # blindly replace it with /arcgis/
        url = url.replace("/ArcGIS/rest/", "/arcgis/")
        # same for uppercase
        url = url.replace("/arcgis/rest/", "/arcgis/")
        # and for old versions
        url = url.replace("ArcX/rest/services", "arcx/services")
        # in uppercase or lowercase
        url = url.replace("arcx/rest/services", "arcx/services")

        srs = 'EPSG:3857'
        bbox_srs = 'EPSG:4326'

        default_source = {
            'type': 'arcgis',
            'req': {
                'url': url.split('?')[0],
                'grid': 'default_grid',
                'transparent': True,
            },
        }

    # A source is the WMS config
    sources = {
        'default_source': default_source
    }

    # A grid is where it will be projects (Mercator in our case)
    grids = {
        'default_grid': {
            'tile_size': [256, 256],
            'srs': grid_srs,
            'origin': 'nw',
        }
    }

    # A cache that does not store for now. It needs a grid and a source.
    caches = {
        'default_cache': {
            'disable_storage': True,
            'grids': ['default_grid'],
            'sources': ['default_source']
        },
    }

    # The layer is connected to the cache
    layers = [
        {'name': layer_name,
         'sources': ['default_cache'],
         'title': "%s" % layer.title,
         },
    ]

    # Services expose all layers.
    # WMS is used for reprojecting
    # TMS is used for easy tiles
    # Demo is used to test our installation, may be disabled in final version
    services = {
        'wms': {
            'image_formats': ['image/png'],
            'md': {
                'abstract': 'This is the Harvard HyperMap Proxy.',
                'title': 'Harvard HyperMap Proxy'
            },
            'srs': ['EPSG:4326', 'EPSG:3857'],
            'srs_bbox': 'EPSG:4326',
            'bbox': bbox,
            'versions': ['1.1.1']
        },
        'wmts': {
            'restful': True,
            'restful_template':
            '/{Layer}/{TileMatrixSet}/{TileMatrix}/{TileCol}/{TileRow}.png',
        },
        'tms': {
            'origin': 'nw',
        },
        'demo': None,
    }

    global_config = {
        'http': {
            'ssl_no_cert_checks': True
        },
    }

    # Populate a dictionary with custom config changes
    extra_config = {
        'caches': caches,
        'grids': grids,
        'layers': layers,
        'services': services,
        'sources': sources,
        'globals': global_config,
    }

    yaml_config = yaml.dump(extra_config, default_flow_style=False)
    # If you want to test the resulting configuration. Turn on the next
    # line and use that to generate a yaml config.
    # assert False

    conf = configure_mapproxy(extra_config)
    # Create a MapProxy App
    app = MapProxyApp(conf.configured_services(), conf.base_config)

    # Wrap it in an object that allows to get requests by path as a string.
    if(config_as_yaml):
        return app, yaml_config

    return app, extra_config


def environ_from_url(path):
    """From webob.request
    TOD: Add License.
    """
    scheme = 'http'
    netloc = 'localhost:80'
    if path and '?' in path:
        path_info, query_string = path.split('?', 1)
        path_info = url_unquote(path_info)
    else:
        path_info = url_unquote(path)
        query_string = ''
    env = {
        'REQUEST_METHOD': 'GET',
        'SCRIPT_NAME': '',
        'PATH_INFO': path_info or '',
        'QUERY_STRING': query_string,
        'SERVER_NAME': netloc.split(':')[0],
        'SERVER_PORT': netloc.split(':')[1],
        'HTTP_HOST': netloc,
        'SERVER_PROTOCOL': 'HTTP/1.0',
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': scheme,
        'wsgi.input': io.BytesIO(),
        'wsgi.errors': sys.stderr,
        'wsgi.multithread': False,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
    }
    return env


def get_path_info_params(yaml_text):
    bbox_req = '-180,-90,180,90'

    if 'services' in yaml_text:
        bbox_req = yaml_text['services']['wms']['bbox']

    if 'layers' in yaml_text:
        lay_name = yaml_text['layers'][0]['name']

    return bbox_req, lay_name


def layer_from_csw(layer_uuid):
    # Get Layer with matching catalog and primary key
    repository = RegistryRepository()
    layer_ids = repository.query_ids([layer_uuid])
    layer = None
    if len(layer_ids) > 0:
        layer = layer_ids[0]

    return layer

## Return the layer as JSON
#
#  @param request Input request from the user.
#  @param layer_uuid Unique identifier of the layer.
#
#  @returns HttpResponse with the JSON content.
#
def layer_json_view(request, layer_uuid):
    layer = layer_from_csw(layer_uuid)
    if not layer:
        return HttpResponse("Layer with uuid {0} not found.".format(layer_uuid), status=404)

    # Set up a mapproxy app for this particular layer
    _, config = get_mapproxy(layer, config_as_yaml=False)
    json_contents = json.dumps(config)

    response = HttpResponse(json_contents, content_type='application/json')

    return response


def layer_yml_view(request, layer_uuid):
    layer = layer_from_csw(layer_uuid)
    if not layer:
        return HttpResponse("Layer with uuid {0} not found.".format(layer_uuid), status=404)

    # Set up a mapproxy app for this particular layer
    _, yaml_config = get_mapproxy(layer)

    response = HttpResponse(yaml_config, content_type='text/plain')

    return response


def layer_xml_view(request, layer_uuid):
    query_string = ('service=CSW&version=3.0.0&request=GetRecordById&elementsetname=full&'
                    'resulttype=results&id={0}'.format(layer_uuid))
    request.META['QUERY_STRING'] = query_string
    response = csw_view(request)

    return response


def layer_png_view(request, layer_uuid):
    layer = layer_from_csw(layer_uuid)
    if not layer:
        return HttpResponse("Layer with uuid {0} not found.".format(layer_uuid), status=404)

    # Set up a mapproxy app for this particular layer
    mp, yaml_config = get_mapproxy(layer)
    yaml_text = yaml.load(yaml_config)

    captured = []
    output = []
    bbox_req, lay_name = get_path_info_params(yaml_text)

    path_info = ('/service?LAYERS={0}&FORMAT=image%2Fpng&SRS=EPSG%3A4326'
                 '&EXCEPTIONS=application%2Fvnd.ogc.se_inimage&TRANSPARENT=TRUE&SERVICE=WMS&VERSION=1.1.1&'
                 'REQUEST=GetMap&STYLES=&BBOX={1}&WIDTH=200&HEIGHT=150').format(lay_name, bbox_req)

    def start_response(status, headers, exc_info=None):
        captured[:] = [status, headers, exc_info]
        return output.append

    # Get a response from MapProxyAppy as if it was running standalone.
    environ = environ_from_url(path_info)
    app_iter = mp(environ, start_response)

    response = HttpResponse(next(app_iter), content_type='image/png')

    return response


def layer_mapproxy(request, layer_uuid, path_info):
    layer = layer_from_csw(layer_uuid)
    if not layer:
        return HttpResponse("Layer with uuid {0} not found.".format(layer_uuid), status=404)

    # Set up a mapproxy app for this particular layer
    mp, yaml_config = get_mapproxy(layer)

    query = request.META['QUERY_STRING']

    if len(query) > 0:
        path_info = path_info + '?' + query

    captured = []
    output = []

    def start_response(status, headers, exc_info=None):
        captured[:] = [status, headers, exc_info]
        return output.append

    # Get a response from MapProxyAppy as if it was running standalone.
    environ = environ_from_url(path_info)
    app_iter = mp(environ, start_response)

    status = int(captured[0].split(' ')[0])
    # Create a Django response from the MapProxy WSGI response (app_iter).
    response = HttpResponse(app_iter, status=status)
    # TODO: Append headers to response, in particular content_type is very important
    # as some of the responses are images.

    return response


def create_response_dict(catalog_id, catalog):
    dictionary = {
        'id': catalog_id,
        'slug': catalog,
        'name': catalog,
        'url': None,
        'search_url': '/{0}/api/'.format(catalog)
    }

    return dictionary


def list_catalogs_view(request):
    es, _ = es_connect(url=REGISTRY_SEARCH_URL)

    list_catalogs = es.get('_aliases').keys()
    response_list = [create_response_dict(i, catalog) for i, catalog in enumerate(list_catalogs)]
    message, status = json.dumps(response_list), 200

    if len(list_catalogs) == 0:
        message, status = 'Empty list of catalogs', 404

    response = HttpResponse(message, status=status, content_type='application/json')

    return response


def readme_view(request):
    with open('documentation.md') as f:
        readme = f.readlines()

    del(readme[3:5], readme[8])

    response = HttpResponse(''.join(readme), status=200, content_type='text/plain')

    return response


def load_records(repo, parsed_xml, context):
    """Load metadata records from directory of files to database"""
    xml_records = parsed_xml.xpath('//csw:Insert', namespaces=context.namespaces)[0]
    parsed_records = xml_records.xpath('child::*')
    parsed_records = [metadata.parse_record(context, f, repo)[0] for f in parsed_records]

    [repo.insert(r, 'local', r.insert_date) for r in parsed_records]


urlpatterns = [
    url(r'^$', readme_view),
    url(r'^csw$', csw_view),
    url(r'^api$', search_view),
    url(r'^catalog$', list_catalogs_view),
    url(r'^catalog/(?P<catalog>\w+)/csw$', csw_view),
    url(r'^catalog/(?P<catalog>\w+)/api/$', search_view),
    url(r'^layer/(?P<layer_uuid>[\w]{8}-[\w]{4}-[\w]{4}-[\w]{4}-[\w]{12}).js$', layer_json_view, name="layer_json"),
    url(r'^layer/(?P<layer_uuid>[\w]{8}-[\w]{4}-[\w]{4}-[\w]{4}-[\w]{12}).yml$', layer_yml_view, name="layer_yml"),
    url(r'^layer/(?P<layer_uuid>[\w]{8}-[\w]{4}-[\w]{4}-[\w]{4}-[\w]{12}).png$', layer_png_view, name="layer_png"),
    url(r'^layer/(?P<layer_uuid>[\w]{8}-[\w]{4}-[\w]{4}-[\w]{4}-[\w]{12}).xml$', layer_xml_view, name="layer_xml"),
    url(r'^layer/(?P<layer_uuid>[\w]{8}-[\w]{4}-[\w]{4}-[\w]{4}-[\w]{12})(?P<path_info>/.*)$', layer_mapproxy),
]


if __name__ == '__main__':  # pragma: no cover
    COMMAND = None
    os.environ['DJANGO_SETTINGS_MODULE'] = 'registry'

    if 'pycsw' in sys.argv[:2]:

        OPTS, ARGS = getopt.getopt(sys.argv[2:], 'c:f:ho:p:ru:x:s:t:y')

        xml_dirpath, catalog_slug = None, None
        for o, a in OPTS:
            if o == '-c':
                COMMAND = a
            elif o == '-p':
                xml_dirpath = a
            elif o == '-s':
                catalog_slug = a

        database = PYCSW['repository']['database']
        table = PYCSW['repository']['table']
        home = PYCSW['server']['home']

        available_commands = ['setup_db', 'get_sysprof', 'load_records']

        if COMMAND not in available_commands:
            print('pycsw supports only the following commands: %s' % available_commands)
            sys.exit(1)

        if COMMAND == 'setup_db':
            pycsw_admin.setup_db(database, table, home)

        elif COMMAND == 'get_sysprof':
            print(pycsw_admin.get_sysprof())

        elif COMMAND == 'load_records':
            if os.path.isfile(xml_dirpath):
                files_names = [xml_dirpath]
            elif os.path.isdir(xml_dirpath):
                files_names = [os.path.join(xml_dirpath, f) for f in os.listdir(xml_dirpath)]
            else:
                print('Undefined xml files path in command line input')
                sys.exit(1)
            if not catalog_slug:
                print('Undefined catalog slug in command line input')
                sys.exit(1)

            # Create repository object with catalog slug.
            context = config.StaticContext()
            repo = RegistryRepository(PYCSW['repository']['database'],
                                      context,
                                      table=PYCSW['repository']['table'])
            repo.catalog = catalog_slug

            # Create index with mapping in Elasticsarch.
            create_index(catalog_slug)

            # Parse each xml file and insert records.
            for xml_file in files_names:
                parsed_xml = etree.parse(xml_file, context.parser)
                load_records(repo, parsed_xml, context)

        sys.exit(0)

    management.execute_from_command_line()
