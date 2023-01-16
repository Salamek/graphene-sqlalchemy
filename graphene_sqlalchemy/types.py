import warnings
from collections import OrderedDict
from inspect import isawaitable
from typing import Any, Optional, Type, Union

import sqlalchemy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import ColumnProperty, CompositeProperty, RelationshipProperty
from sqlalchemy.orm.exc import NoResultFound

import graphene
from graphene import Field, InputField
from graphene.relay import Connection, Node
from graphene.types.base import BaseType
from graphene.types.interface import Interface, InterfaceOptions
from graphene.types.objecttype import ObjectType, ObjectTypeOptions
from graphene.types.unmountedtype import UnmountedType
from graphene.types.utils import yank_fields_from_attrs
from graphene.utils.orderedtype import OrderedType

from .converter import (
    convert_sqlalchemy_column,
    convert_sqlalchemy_composite,
    convert_sqlalchemy_hybrid_method,
    convert_sqlalchemy_relationship,
)
from .enums import (
    enum_for_field,
    sort_argument_for_object_type,
    sort_enum_for_object_type,
)
from .filters import (
    BooleanFilter,
    FloatFilter,
    IdFilter,
    IntFilter,
    BaseTypeFilter,
    RelationshipFilter,
    StringFilter,
)
from .registry import Registry, get_global_registry
from .resolvers import get_attr_resolver, get_custom_resolver
from .utils import (
    SQL_VERSION_HIGHER_EQUAL_THAN_1_4,
    get_nullable_type,
    get_query,
    get_session,
    is_mapped_class,
    is_mapped_instance,
)

if SQL_VERSION_HIGHER_EQUAL_THAN_1_4:
    from sqlalchemy.ext.asyncio import AsyncSession


class ORMField(OrderedType):
    def __init__(
        self,
        model_attr=None,
        type_=None,
        required=None,
        description=None,
        deprecation_reason=None,
        batching=None,
        create_filter=None,
        filter_type: Optional[Type] = None,
        _creation_counter=None,
        **field_kwargs,
    ):
        """
        Use this to override fields automatically generated by SQLAlchemyObjectType.
        Unless specified, options will default to SQLAlchemyObjectType usual behavior
        for the given SQLAlchemy model property.

        Usage:
            class MyModel(Base):
                id = Column(Integer(), primary_key=True)
                name = Column(String)

            class MyType(SQLAlchemyObjectType):
                class Meta:
                    model = MyModel

                id = ORMField(type_=graphene.Int)
                name = ORMField(required=True)

        -> MyType.id will be of type Int (vs ID).
        -> MyType.name will be of type NonNull(String) (vs String).

        :param str model_attr:
            Name of the SQLAlchemy model attribute used to resolve this field.
            Default to the name of the attribute referencing the ORMField.
        :param type_:
            Default to the type mapping in converter.py.
        :param str description:
            Default to the `doc` attribute of the SQLAlchemy column property.
        :param bool required:
            Default to the opposite of the `nullable` attribute of the SQLAlchemy column property.
        :param str description:
            Same behavior as in graphene.Field. Defaults to None.
        :param str deprecation_reason:
            Same behavior as in graphene.Field. Defaults to None.
        :param bool batching:
            Toggle SQL batching. Defaults to None, that is `SQLAlchemyObjectType.meta.batching`.
        :param bool create_filter:
            Create a filter for this field. Defaults to True.
        :param Type filter_type:
            Override for the filter of this field with a custom filter type.
            Default behavior is to get a matching filter type for this field from the registry.
            Create_filter needs to be true
        :param int _creation_counter:
            Same behavior as in graphene.Field.
        """
        super(ORMField, self).__init__(_creation_counter=_creation_counter)
        # The is only useful for documentation and auto-completion
        common_kwargs = {
            "model_attr": model_attr,
            "type_": type_,
            "required": required,
            "description": description,
            "deprecation_reason": deprecation_reason,
            "create_filter": create_filter,
            "filter_type": filter_type,
            "batching": batching,
        }
        common_kwargs = {
            kwarg: value for kwarg, value in common_kwargs.items() if value is not None
        }
        self.kwargs = field_kwargs
        self.kwargs.update(common_kwargs)


def get_or_create_relationship_filter(
    base_type: Type[BaseType], registry: Registry
) -> Type[RelationshipFilter]:
    relationship_filter = registry.get_relationship_filter_for_base_type(base_type)

    if not relationship_filter:
        base_type_filter = registry.get_filter_for_base_type(base_type)
        relationship_filter = RelationshipFilter.create_type(
            f"{base_type.__name__}RelationshipFilter",
            base_type_filter=base_type_filter,
            model=base_type._meta.model,
        )
        registry.register_relationship_filter_for_base_type(
            base_type, relationship_filter
        )

    return relationship_filter


def filter_field_from_type_field(
    field: Union[graphene.Field, graphene.Dynamic, Type[UnmountedType]],
    registry: Registry,
    filter_type: Optional[Type],
) -> Optional[Union[graphene.InputField, graphene.Dynamic]]:
    # If a custom filter type was set for this field, use it here
    if filter_type:
        return graphene.InputField(filter_type)
    # fixme one test case fails where, find out why
    if issubclass(type(field), graphene.Scalar):
        filter_class = registry.get_filter_for_scalar_type(type(field))
        return graphene.InputField(filter_class)

    elif isinstance(field.type, graphene.List):
        print("got field with list type")
        pass
    elif isinstance(field, graphene.List):
        print("Got list")
        pass
    elif isinstance(field.type, graphene.Dynamic):
        pass
    # If the field is Dynamic, we don't know its type yet and can't select the right filter
    elif isinstance(field, graphene.Dynamic):

        def resolve_dynamic():
            # Resolve Dynamic Type
            type_ = get_nullable_type(field.get_type())
            from graphene_sqlalchemy import SQLAlchemyConnectionField

            from .fields import UnsortedSQLAlchemyConnectionField

            if isinstance(type_, SQLAlchemyConnectionField) or isinstance(
                type_, UnsortedSQLAlchemyConnectionField
            ):
                inner_type = get_nullable_type(type_.type.Edge.node._type)
                reg_res = get_or_create_relationship_filter(inner_type, registry)
                if not reg_res:
                    print("filter class was none!!!")
                    print(type_)
                return graphene.InputField(reg_res)
            elif isinstance(type_, Field):
                if isinstance(type_.type, graphene.List):
                    inner_type = get_nullable_type(type_.type.of_type)
                    reg_res = get_or_create_relationship_filter(inner_type, registry)
                    if not reg_res:
                        print("filter class was none!!!")
                        print(type_)
                    return graphene.InputField(reg_res)
                reg_res = registry.get_filter_for_base_type(type_.type)

                return graphene.InputField(reg_res)
            else:
                warnings.warn(f"Unexpected Dynamic Type: {type_}")  # Investigate
                # raise Exception(f"Unexpected Dynamic Type: {type_}")

        return graphene.Dynamic(resolve_dynamic)

    elif isinstance(field, graphene.Field):
        type_ = get_nullable_type(field.type)
        # Field might be a SQLAlchemyObjectType, due to hybrid properties
        if issubclass(type_, SQLAlchemyObjectType):
            filter_class = registry.get_filter_for_base_type(type_)
            return graphene.InputField(filter_class)
        filter_class = registry.get_filter_for_scalar_type(type_)
        if not filter_class:
            warnings.warn(
                f"No compatible filters found for {field.type}. Skipping field."
            )
            return None
        return graphene.InputField(filter_class)
    else:
        raise Exception(
            f"Expected a graphene.Field or graphene.Dynamic, but got: {field}"
        )


def get_polymorphic_on(model):
    """
    Check whether this model is a polymorphic type, and if so return the name
    of the discriminator field (`polymorphic_on`), so that it won't be automatically
    generated as an ORMField.
    """
    if hasattr(model, "__mapper__") and model.__mapper__.polymorphic_on is not None:
        polymorphic_on = model.__mapper__.polymorphic_on
        if isinstance(polymorphic_on, sqlalchemy.Column):
            return polymorphic_on.name


def construct_fields_and_filters(
    obj_type,
    model,
    registry,
    only_fields,
    exclude_fields,
    batching,
    create_filters,
    connection_field_factory,
):
    """
    Construct all the fields for a SQLAlchemyObjectType.
    The main steps are:
      - Gather all the relevant attributes from the SQLAlchemy model
      - Gather all the ORM fields defined on the type
      - Merge in overrides and build up all the fields

    :param SQLAlchemyObjectType obj_type:
    :param model: the SQLAlchemy model
    :param Registry registry:
    :param tuple[string] only_fields:
    :param tuple[string] exclude_fields:
    :param bool batching:
    :param bool create_filters: Enable filter generation for this type
    :param function|None connection_field_factory:
    :rtype: OrderedDict[str, graphene.Field]
    """
    inspected_model = sqlalchemy.inspect(model)
    # Gather all the relevant attributes from the SQLAlchemy model in order
    all_model_attrs = OrderedDict(
        inspected_model.column_attrs.items()
        + inspected_model.composites.items()
        + [
            (name, item)
            for name, item in inspected_model.all_orm_descriptors.items()
            if isinstance(item, hybrid_property)
        ]
        + inspected_model.relationships.items()
    )

    # Filter out excluded fields
    polymorphic_on = get_polymorphic_on(model)
    auto_orm_field_names = []
    for attr_name, attr in all_model_attrs.items():
        if (
            (only_fields and attr_name not in only_fields)
            or (attr_name in exclude_fields)
            or attr_name == polymorphic_on
        ):
            continue
        auto_orm_field_names.append(attr_name)

    # Gather all the ORM fields defined on the type
    custom_orm_fields_items = [
        (attn_name, attr)
        for base in reversed(obj_type.__mro__)
        for attn_name, attr in base.__dict__.items()
        if isinstance(attr, ORMField)
    ]
    custom_orm_fields_items = sorted(custom_orm_fields_items, key=lambda item: item[1])

    # Set the model_attr if not set
    for orm_field_name, orm_field in custom_orm_fields_items:
        attr_name = orm_field.kwargs.get("model_attr", orm_field_name)
        if attr_name not in all_model_attrs:
            raise ValueError(
                ("Cannot map ORMField to a model attribute.\n" "Field: '{}.{}'").format(
                    obj_type.__name__,
                    orm_field_name,
                )
            )
        orm_field.kwargs["model_attr"] = attr_name

    # Merge automatic fields with custom ORM fields
    orm_fields = OrderedDict(custom_orm_fields_items)
    for orm_field_name in auto_orm_field_names:
        if orm_field_name in orm_fields:
            continue
        orm_fields[orm_field_name] = ORMField(model_attr=orm_field_name)

    # Build all the field dictionary
    fields = OrderedDict()
    filters = OrderedDict()
    for orm_field_name, orm_field in orm_fields.items():
        filtering_enabled_for_field = orm_field.kwargs.pop(
            "create_filter", create_filters
        )
        filter_type = orm_field.kwargs.pop("filter_type", None)
        attr_name = orm_field.kwargs.pop("model_attr")
        attr = all_model_attrs[attr_name]
        resolver = get_custom_resolver(obj_type, orm_field_name) or get_attr_resolver(
            obj_type, attr_name
        )

        if isinstance(attr, ColumnProperty):
            field = convert_sqlalchemy_column(
                attr, registry, resolver, **orm_field.kwargs
            )
        elif isinstance(attr, RelationshipProperty):
            batching_ = orm_field.kwargs.pop("batching", batching)
            field = convert_sqlalchemy_relationship(
                attr,
                obj_type,
                connection_field_factory,
                batching_,
                orm_field_name,
                **orm_field.kwargs,
            )
        elif isinstance(attr, CompositeProperty):
            if attr_name != orm_field_name or orm_field.kwargs:
                # TODO Add a way to override composite property fields
                raise ValueError(
                    "ORMField kwargs for composite fields must be empty. "
                    "Field: {}.{}".format(obj_type.__name__, orm_field_name)
                )
            field = convert_sqlalchemy_composite(attr, registry, resolver)
        elif isinstance(attr, hybrid_property):
            field = convert_sqlalchemy_hybrid_method(attr, resolver, **orm_field.kwargs)
        else:
            raise Exception("Property type is not supported")  # Should never happen

        registry.register_orm_field(obj_type, orm_field_name, attr)
        fields[orm_field_name] = field
        if filtering_enabled_for_field:
            filters[orm_field_name] = filter_field_from_type_field(
                field, registry, filter_type
            )

    return fields, filters


class SQLAlchemyObjectTypeOptions(ObjectTypeOptions):
    model = None  # type: sqlalchemy.Model
    registry = None  # type: sqlalchemy.Registry
    connection = None  # type: sqlalchemy.Type[sqlalchemy.Connection]
    id = None  # type: str
    filter_class: Type[BaseTypeFilter] = None


class SQLAlchemyBase(BaseType):
    """
    This class contains initialization code that is common to both ObjectTypes
    and Interfaces.  You typically don't need to use it directly.
    """

    @classmethod
    def __init_subclass_with_meta__(
        cls,
        model=None,
        registry=None,
        skip_registry=False,
        only_fields=(),
        exclude_fields=(),
        connection=None,
        connection_class=None,
        use_connection=None,
        interfaces=(),
        id=None,
        batching=False,
        connection_field_factory=None,
        _meta=None,
        **options,
    ):
        # We always want to bypass this hook unless we're defining a concrete
        # `SQLAlchemyObjectType` or `SQLAlchemyInterface`.
        if not _meta:
            return

        # Make sure model is a valid SQLAlchemy model
        if not is_mapped_class(model):
            raise ValueError(
                "You need to pass a valid SQLAlchemy Model in "
                '{}.Meta, received "{}".'.format(cls.__name__, model)
            )

        if not registry:
            registry = get_global_registry()
            # TODO way of doing this automatically?
            get_global_registry().register_filter_for_scalar_type(
                graphene.Float, FloatFilter
            )
            get_global_registry().register_filter_for_scalar_type(
                graphene.Int, IntFilter
            )
            get_global_registry().register_filter_for_scalar_type(
                graphene.String, StringFilter
            )
            get_global_registry().register_filter_for_scalar_type(
                graphene.Boolean, BooleanFilter
            )
            get_global_registry().register_filter_for_scalar_type(graphene.ID, IdFilter)

        assert isinstance(registry, Registry), (
            "The attribute registry in {} needs to be an instance of "
            'Registry, received "{}".'
        ).format(cls.__name__, registry)

        if only_fields and exclude_fields:
            raise ValueError(
                "The options 'only_fields' and 'exclude_fields' cannot be both set on the same type."
            )

        fields, filters = construct_fields_and_filters(
            obj_type=cls,
            model=model,
            registry=registry,
            only_fields=only_fields,
            exclude_fields=exclude_fields,
            batching=batching,
            create_filters=True,
            connection_field_factory=connection_field_factory,
        )

        sqla_fields = yank_fields_from_attrs(
            fields,
            _as=Field,
            sort=False,
        )

        if use_connection is None and interfaces:
            use_connection = any(
                issubclass(interface, Node) for interface in interfaces
            )

        if use_connection and not connection:
            # We create the connection automatically
            if not connection_class:
                connection_class = Connection

            connection = connection_class.create_type(
                "{}Connection".format(cls.__name__), node=cls
            )

        if connection is not None:
            assert issubclass(connection, Connection), (
                "The connection must be a Connection. Received {}"
            ).format(connection.__name__)

        _meta.model = model
        _meta.registry = registry

        if _meta.fields:
            _meta.fields.update(sqla_fields)
        else:
            _meta.fields = sqla_fields

        # Save Generated filter class in Meta Class
        if not _meta.filter_class:
            # Map graphene fields to filters
            # TODO we might need to pass the ORMFields containing the SQLAlchemy models
            #  to the scalar filters here (to generate expressions from the model)

            filter_fields = yank_fields_from_attrs(filters, _as=InputField, sort=False)

            _meta.filter_class = BaseTypeFilter.create_type(
                f"{cls.__name__}Filter", filter_fields=filter_fields, model=model
            )
            registry.register_filter_for_base_type(cls, _meta.filter_class)

        _meta.connection = connection
        _meta.id = id or "id"

        cls.connection = connection  # Public way to get the connection

        super(SQLAlchemyBase, cls).__init_subclass_with_meta__(
            _meta=_meta, interfaces=interfaces, **options
        )

        if not skip_registry:
            registry.register(cls)

    @classmethod
    def is_type_of(cls, root, info):
        if isinstance(root, cls):
            return True
        if isawaitable(root):
            raise Exception(
                "Received coroutine instead of sql alchemy model. "
                "You seem to use an async engine with synchronous schema execution"
            )
        if not is_mapped_instance(root):
            raise Exception(('Received incompatible instance "{}".').format(root))
        return isinstance(root, cls._meta.model)

    @classmethod
    def get_query(cls, info):
        model = cls._meta.model
        return get_query(model, info.context)

    @classmethod
    def get_node(cls, info, id):
        if not SQL_VERSION_HIGHER_EQUAL_THAN_1_4:
            try:
                return cls.get_query(info).get(id)
            except NoResultFound:
                return None

        session = get_session(info.context)
        if isinstance(session, AsyncSession):

            async def get_result() -> Any:
                return await session.get(cls._meta.model, id)

            return get_result()
        try:
            return cls.get_query(info).get(id)
        except NoResultFound:
            return None

    def resolve_id(self, info):
        # graphene_type = info.parent_type.graphene_type
        keys = self.__mapper__.primary_key_from_instance(self)
        return tuple(keys) if len(keys) > 1 else keys[0]

    @classmethod
    def enum_for_field(cls, field_name):
        return enum_for_field(cls, field_name)

    @classmethod
    def get_filter_argument(cls):
        if cls._meta.filter_class:
            return graphene.Argument(cls._meta.filter_class)
        return None

    sort_enum = classmethod(sort_enum_for_object_type)

    sort_argument = classmethod(sort_argument_for_object_type)


class SQLAlchemyObjectTypeOptions(ObjectTypeOptions):
    model = None  # type: sqlalchemy.Model
    registry = None  # type: sqlalchemy.Registry
    connection = None  # type: sqlalchemy.Type[sqlalchemy.Connection]
    id = None  # type: str
    filter_class: Type[BaseTypeFilter] = None


class SQLAlchemyObjectType(SQLAlchemyBase, ObjectType):
    """
    This type represents the GraphQL ObjectType. It reflects on the
    given SQLAlchemy model, and automatically generates an ObjectType
    using the column and relationship information defined there.

    Usage:

        class MyModel(Base):
            id = Column(Integer(), primary_key=True)
            name = Column(String())

        class MyType(SQLAlchemyObjectType):
            class Meta:
                model = MyModel
    """

    @classmethod
    def __init_subclass_with_meta__(cls, _meta=None, **options):
        if not _meta:
            _meta = SQLAlchemyObjectTypeOptions(cls)

        super(SQLAlchemyObjectType, cls).__init_subclass_with_meta__(
            _meta=_meta, **options
        )


class SQLAlchemyInterfaceOptions(InterfaceOptions):
    model = None  # type: sqlalchemy.Model
    registry = None  # type: sqlalchemy.Registry
    connection = None  # type: sqlalchemy.Type[sqlalchemy.Connection]
    id = None  # type: str
    filter_class: Type[BaseTypeFilter] = None


class SQLAlchemyInterface(SQLAlchemyBase, Interface):
    """
    This type represents the GraphQL Interface. It reflects on the
    given SQLAlchemy model, and automatically generates an Interface
    using the column and relationship information defined there. This
    is used to construct interface relationships based on polymorphic
    inheritance hierarchies in SQLAlchemy.

    Please note that by default, the "polymorphic_on" column is *not*
    generated as a field on types that use polymorphic inheritance, as
    this is considered an implentation detail. The idiomatic way to
    retrieve the concrete GraphQL type of an object is to query for the
    `__typename` field.

    Usage (using joined table inheritance):

        class MyBaseModel(Base):
            id = Column(Integer(), primary_key=True)
            type = Column(String())
            name = Column(String())

        __mapper_args__ = {
            "polymorphic_on": type,
        }

        class MyChildModel(Base):
            date = Column(Date())

        __mapper_args__ = {
            "polymorphic_identity": "child",
        }

        class MyBaseType(SQLAlchemyInterface):
            class Meta:
                model = MyBaseModel

        class MyChildType(SQLAlchemyObjectType):
            class Meta:
                model = MyChildModel
                interfaces = (MyBaseType,)
    """

    @classmethod
    def __init_subclass_with_meta__(cls, _meta=None, **options):
        if not _meta:
            _meta = SQLAlchemyInterfaceOptions(cls)

        super(SQLAlchemyInterface, cls).__init_subclass_with_meta__(
            _meta=_meta, **options
        )

        # make sure that the model doesn't have a polymorphic_identity defined
        if hasattr(_meta.model, "__mapper__"):
            polymorphic_identity = _meta.model.__mapper__.polymorphic_identity
            assert (
                polymorphic_identity is None
            ), '{}: An interface cannot map to a concrete type (polymorphic_identity is "{}")'.format(
                cls.__name__, polymorphic_identity
            )
