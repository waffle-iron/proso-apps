import logging
from proso.django.response import render
import proso_common.views
from time import time as time_lib
from models import DecoratedAnswer
from proso.django.cache import cache_page_conditional
import proso_common.json_enrich as common_json_enrich
from proso.django.request import is_user_id_overriden
import json_enrich


LOGGER = logging.getLogger('django.request')


def home(request):
    return render(request, 'flashcards_home.html', {})


@cache_page_conditional(condition=lambda request: 'stats' not in request.GET)
def show_one(request, object_class, id):
    return proso_common.views.show_one(
        request, _to_json, object_class, id, template='flashcards_json.html')


@cache_page_conditional(condition=lambda request: 'stats' not in request.GET)
def show_more(request, object_class, should_cache=True):
    def _load_objects(request, object_class):
        select_related_all = {}
        prefetch_related_all = {}
        select_related = select_related_all.get(object_class, [])
        prefetch_related = prefetch_related_all.get(object_class, [])
        objs = object_class.objects.select_related(*select_related).\
            prefetch_related(*prefetch_related).all()
        if object_class == DecoratedAnswer:
            if is_user_id_overriden(request):
                user_id = int(request.GET['user'])
            else:
                user_id = request.user.id
            objs = objs.filter(general_answer__user_id=user_id).order_by('-general_answer__time')
        return objs
    return proso_common.views.show_more(
        request, _to_json, _load_objects, object_class,
        should_cache=should_cache, template='flashcards_json.html')


def _to_json(request, value):
    time_start = time_lib()
    if isinstance(value, list):
        json = map(lambda x: x if isinstance(x, dict) else x.to_json(), value)
    elif not isinstance(value, dict):
        json = value.to_json()
    else:
        json = value
    common_json_enrich.enrich_by_predicate(request, json, json_enrich.url, lambda x: True)
    LOGGER.debug("converting value to simple JSON took %s seconds", (time_lib() - time_start))
    return json
