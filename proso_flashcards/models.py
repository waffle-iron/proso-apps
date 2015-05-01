from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models import Q
import itertools
from proso_models.models import Item, Answer, get_environment, get_item_selector, get_option_selector
from django.db.models.signals import pre_save, m2m_changed, post_save, pre_delete
from django.dispatch import receiver
from proso.django.util import disable_for_loaddata

CACHE_EXPIRATION = 60 * 60 * 24 * 30


class Term(models.Model):
    identifier = models.SlugField()
    item = models.ForeignKey(Item, null=True, default=None, related_name="flashcard_terms")

    lang = models.CharField(max_length=2)
    name = models.TextField()
    type = models.CharField(max_length=50, null=True, blank=True)

    def to_json(self, nested=False):
        json = {
            "id": self.pk,
            "identifier": self.identifier,
            "item_id": self.item_id,
            "object_type": "fc_term",
            "lang": self.lang,
            "name": self.name,
            "type": self.type,
        }
        if not nested:
            json["parents"] = [parent.to_json(nested=True) for parent in self.parents.all()]
        return json

    def __unicode__(self):
        return u"{0.lang} - {0.name}".format(self)


class Context(models.Model):
    identifier = models.SlugField()
    item = models.ForeignKey(Item, null=True, default=None, related_name="flashcard_contexts")

    lang = models.CharField(max_length=2)
    name = models.TextField(null=True, blank=True)
    content = models.TextField(null=True, blank=True)

    def to_json(self, nested=False):
        json = {
            "id": self.pk,
            "identifier": self.identifier,
            "item_id": self.item_id,
            "object_type": "fc_context",
            "lang": self.lang,
            "name": self.name,
            "content": self.content,
        }
        if not nested:
            json["categories"] = [category.to_json(nested=True) for category in self.categories.all()]
        return json

    def __unicode__(self):
        return u"{0.lang} - {0.name}".format(self)


class FlashcardManager(models.Manager):
    def candidates(self, categories, contexts, types, avoid):
        qs = self.filter(Q(active=True) & ~Q(id__in=avoid))
        if isinstance(contexts, list) and len(contexts) > 0:
            qs = qs.filter(reduce(lambda a, b: a | b, map(lambda id:
                    Q(context_id=id) if isinstance(id, int) else Q(context__identifier=id), contexts)))
        if isinstance(categories, list) and len(categories) > 0:
            qs = qs.filter(reduce(lambda a, b: a | b, map(lambda id:
                Q(term__parents__id=id) | Q(categories__id=id) | Q(context__categories__id=id) if isinstance(id, int)
                else Q(term__parents__identifier=id) | Q(categories__identifier=id) |
                Q(context__categories__identifier=id), categories)))
        if isinstance(types, list) and len(types) > 0:
            qs = qs.filter(reduce(lambda a, b: a | b, map(lambda type: Q(term__type=type), types)))
        return qs

    def practice(self, environment, user, time, limit, flashcard_qs, language=None, with_contexts=True):
        # prepare
        item_selector = get_item_selector()
        option_selector = get_option_selector(item_selector)
        items = list(flashcard_qs.filter(lang=language).order_by("?")[:100].values_list("item_id", flat=True))

        selected_items = item_selector.select(environment, user, items, time, limit)

        # get selected flashcards
        flashcards = Flashcard.objects.filter(item_id__in=selected_items).prefetch_related(Flashcard.related_term())
        if with_contexts:
            flashcards = flashcards.prefetch_related(Flashcard.related_context())
        if language is not None:
            flashcards = flashcards.filter(lang=language)
        flashcards = sorted(flashcards, key=lambda fc: selected_items.index(fc.item_id))

        from proso_flashcards.flashcard_construction import get_option_set, get_direction

        # select direction
        direction = get_direction()
        allow_zero_option = {}
        for flashcard in flashcards:
            flashcard.direction = direction.get_direction(flashcard)
            allow_zero_option[flashcard.item_id] = flashcard.direction == FlashcardAnswer.FROM_TERM

        # select options
        optionSets = get_option_set().get_option_for_flashcards(flashcards)
        options = option_selector.select_options_more_items(environment, user, selected_items, time, optionSets,
                                                allow_zero_options=allow_zero_option)
        all_options = {}
        for option in Flashcard.objects.filter(lang=language, item_id__in=set(itertools.chain(*options))) \
                .prefetch_related("term", "context"):
            all_options[option.item_id] = option
        options = dict(zip(selected_items, options))

        for flashcard in flashcards:
            if len(options[flashcard.item_id]) > 0:
                flashcard.options = map(lambda id: all_options[id], options[flashcard.item_id])

        return flashcards

    def under_categories_as_items(self, categories):
        key = "fc: category_subitems:" + ",".join(map(str, sorted(categories)))
        items = cache.get(key)
        if items is None:
            items = list(self.under_categories(categories).values_list("item_id", flat=True))
            cache.set(key, items, CACHE_EXPIRATION)

        return items

    def under_categories(self, categories):
        all_categories = Category.objects.subcategories(categories)
        return self.filter(
            Q(categories__pk__in=all_categories) |
            Q(context__pk__in=Category.objects.subcontexts(all_categories)) |
            Q(term__pk__in=Category.objects.subterms(all_categories))
        )


class Flashcard(models.Model):
    identifier = models.SlugField()
    item = models.ForeignKey(Item, null=True, default=None, related_name="flashcards")

    lang = models.CharField(max_length=2)
    term = models.ForeignKey(Term, related_name="flashcards")
    context = models.ForeignKey(Context, related_name="flashcards")
    description = models.TextField(null=True)
    active = models.BooleanField(default=True)

    objects = FlashcardManager()

    def to_json(self, nested=False, categories=True, contexts=True):
        data = {
            "id": self.pk,
            "identifier": self.identifier,
            "item_id": self.item_id,
            "object_type": "fc_flashcard",
            "active": self.active,
            "lang": self.lang,
            "term": self.get_term().to_json(nested=True),
            "description": self.description
        }
        if hasattr(self, "options"):
            data["options"] = map(lambda o: o.to_json(nested=True), self.options)
        if hasattr(self, "direction"):
            data["direction"] = self.direction
        if not nested and categories:
            data["categories"] = [category.to_json(nested=True) for category in self.categories.all()]
        if not nested and contexts:
            data["context"] = self.get_context().to_json(nested=True)
        else:
            data["context_id"] = self.context_id
        return data

    def get_term(self):
        extension = settings.PROSO_FLASHCARDS.get("term_extension", None)
        if extension is None:
            return self.term
        else:
            return getattr(self.term, extension.__name__.lower())

    def get_context(self):
        extension = settings.PROSO_FLASHCARDS.get("context_extension", None)
        if extension is None:
            return self.context
        else:
            return getattr(self.context, extension.__name__.lower())

    @staticmethod
    def related_term():
        extension = settings.PROSO_FLASHCARDS.get("term_extension", None)
        if extension is None:
            return "term"
        else:
            return "term__{}".format(extension.__name__.lower())

    @staticmethod
    def related_context():
        extension = settings.PROSO_FLASHCARDS.get("context_extension", None)
        if extension is None:
            return "context"
        else:
            return "context__{}".format(extension.__name__.lower())

    def __unicode__(self):
        return u"{0.term} - {0.context}".format(self)


class CategoryManager(models.Manager):
    def subcategories(self, categories):
        subcategories = set(categories)
        while len(categories) > 0:
            categories = self.filter(parents__pk__in=categories).values_list("pk", flat=True)
            subcategories |= set(categories)
        return list(subcategories)

    def subcontexts(self, categories):
        return Context.objects.filter(categories__pk__in=categories)

    def subterms(self, categories):
        return Term.objects.filter(parents__pk__in=categories)


class Category(models.Model):
    class Meta:
        verbose_name_plural = "categories"

    CATEGORIES = "c"
    TERMS = "t"
    FLASHCARDS = "f"
    CONTEXTS = "x"
    TYPES = (
        (CATEGORIES, "categories"),
        (TERMS, "terms"),
        (FLASHCARDS, "flashcards"),
        (CONTEXTS, "contexts"),
    )

    identifier = models.SlugField()
    item = models.ForeignKey(Item, null=True, default=None, related_name="flashcard_categories")

    lang = models.CharField(max_length=2)
    name = models.TextField()
    type = models.CharField(max_length=50, null=True, blank=True)
    subcategories = models.ManyToManyField("self", related_name="parents", symmetrical=False)
    terms = models.ManyToManyField(Term, related_name="parents")
    flashcards = models.ManyToManyField(Flashcard, related_name="categories")
    contexts = models.ManyToManyField(Context, related_name="categories")
    not_in_model = models.BooleanField(default=False)
    children_type = models.CharField(max_length=1, choices=TYPES, null=True, verbose_name="Category of")

    objects = CategoryManager()

    def to_json(self, nested=False):
        return {
            "id": self.pk,
            "identifier": self.identifier,
            "item_id": self.item_id,
            "object_type": "fc_category",
            "lang": self.lang,
            "name": self.name,
            "type": self.type,
            "not-in-model": self.not_in_model,
        }

    def __unicode__(self):
        return u"{0.lang} - {0.name}".format(self)


class FlashcardAnswer(Answer):
    FROM_TERM = "t2d"
    FROM_DESCRIPTION = "d2t"
    DIRECTIONS = (
        (FROM_TERM, "From term to description"),
        (FROM_DESCRIPTION, "From description to term"),
    )

    direction = models.CharField(choices=DIRECTIONS, max_length=3)
    options = models.ManyToManyField(Flashcard, related_name="answers_with_this_as_option")
    meta = models.TextField(null=True, blank=True)

    def to_json(self, nested=False):
        json = Answer.to_json(self)
        json['direction'] = self.direction
        json['meta'] = self.meta
        json['object_type'] = "fc_answer"
        if not nested:
            json["options"] = [flashcard.to_json(nested=True) for flashcard in self.options.all()]
        return json


@receiver(pre_save, sender=Term)
@receiver(pre_save, sender=Context)
@receiver(pre_save, sender=Flashcard)
@receiver(pre_save, sender=Category)
@disable_for_loaddata
def create_items(sender, instance, **kwargs):
    if instance.item_id is None and instance.item is None:
        item = Item()
        item.save()
        instance.item = item


PROSO_MODELS_TO_EXPORT = [Category, Flashcard, FlashcardAnswer,
                          settings.PROSO_FLASHCARDS.get("context_extension", Context),
                          settings.PROSO_FLASHCARDS.get("term_extension", Term)]


@receiver(m2m_changed, sender=Category.terms.through)
@receiver(m2m_changed, sender=Category.subcategories.through)
@disable_for_loaddata
def update_parents(sender, instance, action, reverse, model, pk_set, **kwargs):
    environment = get_environment()
    parent_items = []
    child_items = []

    if action == "pre_clear":
        if not reverse:
            parent_items = [instance.item_id]
            children = instance.terms if model == Term else instance.subcategories
            child_items = children.all().values_list("item_id", flat=True)
        else:
            parent_items = instance.parents.all().values_list("item_id", flat=True)
            child_items = [instance.item_id]

    if action == "post_add" and not reverse and instance.not_in_model:
        return

    if action == "post_add" or action == "post_remove":
        if not reverse:
            parent_items = [instance.item_id]
            child_items = model.objects.filter(pk__in=pk_set).values_list("item_id", flat=True)
        else:
            parent_items = Category.objects.filter(pk__in=pk_set, not_in_model=False).values_list("item_id", flat=True)
            child_items = [instance.item_id]

    if action == "post_add":
        for parent_item in parent_items:
            for child_item in child_items:
                environment.write("child", 1, item=parent_item, item_secondary=child_item, symmetric=False,
                                  permanent=True)
                environment.write("parent", 1, item=child_item, item_secondary=parent_item, symmetric=False,
                                  permanent=True)
        return

    if action == "post_remove" or "pre_clear":
        for parent_item in parent_items:
            for child_item in child_items:
                environment.delete("child", item=parent_item, item_secondary=child_item, symmetric=False)
                environment.delete("parent", item=child_item, item_secondary=parent_item, symmetric=False)
        return


@receiver(post_save, sender=Flashcard)
@disable_for_loaddata
def add_parent(sender, instance, **kwargs):
    environment = get_environment()
    parent = instance.term.item_id
    child = instance.item_id
    environment.write("child", 1, item=parent, item_secondary=child, symmetric=False, permanent=True)
    environment.write("parent", 1, item=child, item_secondary=parent, symmetric=False, permanent=True)


@receiver(pre_delete, sender=Flashcard)
@disable_for_loaddata
def delete_parent(sender, instance, **kwargs):
    environment = get_environment()
    parent = instance.term.item_id
    child = instance.item_id
    environment.delete("child", 1, item=parent, item_secondary=child, symmetric=False)
    environment.delete("parent", 1, item=child, item_secondary=parent, symmetric=False)
