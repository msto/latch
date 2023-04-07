import json
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Type,
    TypedDict,
    Union,
    overload,
)

import gql
import graphql.language as l
import graphql.type as t
import graphql.utilities as u
from typing_extensions import override

from latch.gql._execute import execute
from latch.registry.record import Record
from latch.registry.types import InvalidValue, RecordValue
from latch.registry.upstream_types.types import DBType
from latch.registry.upstream_types.values import DBValue, EmptyCell
from latch.registry.utils import (
    RegistryPythonValue,
    RegistryTransformerException,
    to_python_literal,
    to_python_type,
    to_registry_literal,
)


class _AllRecordsNode(TypedDict):
    sampleId: str
    sampleName: str
    sampleDataKey: str
    sampleDataValue: DBValue


class _ColumnNode(TypedDict):
    key: str
    type: DBType


@dataclass(frozen=True)
class Column:
    key: str
    type: Union[Type[RegistryPythonValue], Type[Union[RegistryPythonValue, EmptyCell]]]
    upstream_type: DBType


@dataclass
class _Cache:
    display_name: Optional[str] = None
    columns: Optional[Dict[str, Column]] = None


@dataclass(frozen=True)
class Table:
    _cache: _Cache = field(
        default_factory=lambda: _Cache(),
        init=False,
        repr=False,
        hash=False,
        compare=False,
    )

    id: str

    def load(self):
        data = execute(
            gql.gql("""
                query TableQuery($id: BigInt!) {
                    catalogExperiment(id: $id) {
                        id
                        displayName
                        catalogExperimentColumnDefinitionsByExperimentId {
                            nodes {
                                key
                                type
                            }
                        }
                    }
                }
                """),
            variables={"id": self.id},
        )["catalogExperiment"]
        # todo(maximsmol): deal with nonexistent tables

        self._cache.display_name = data["displayName"]

        self._cache.columns = {}
        columns: List[_ColumnNode] = data[
            "catalogExperimentColumnDefinitionsByExperimentId"
        ]["nodes"]
        for x in columns:
            py_type = to_python_type(x["type"]["type"])
            if x["type"]["allowEmpty"]:
                py_type = Union[py_type, EmptyCell]

            cur = Column(x["key"], py_type, x["type"])
            self._cache.columns[cur.key] = cur

    @overload
    def get_display_name(self, *, load_if_missing: Literal[False]) -> Optional[str]:
        ...

    @overload
    def get_display_name(self, *, load_if_missing: Literal[True] = True) -> str:
        ...

    def get_display_name(self, *, load_if_missing: bool = True) -> Optional[str]:
        if self._cache.display_name is None and load_if_missing:
            self.load()

        return self._cache.display_name

    @overload
    def get_columns(
        self, *, load_if_missing: Literal[False]
    ) -> Optional[Dict[str, Column]]:
        ...

    @overload
    def get_columns(
        self, *, load_if_missing: Literal[True] = True
    ) -> Dict[str, Column]:
        ...

    def get_columns(
        self, *, load_if_missing: bool = True
    ) -> Optional[Dict[str, Column]]:
        if self._cache.columns is None and load_if_missing:
            self.load()

        return self._cache.columns

    def list_records(self, *, page_size: int = 100) -> Iterator[Dict[str, Record]]:
        cols = self.get_columns()
        col_types: Dict[str, DBType] = {c.key: c.upstream_type for c in cols.values()}

        # todo(maximsmol): because allSamples returns each column as its own
        # row, we can't paginate by samples because we don't know when a sample is finished
        nodes: List[_AllRecordsNode] = execute(
            gql.gql("""
                query TableQuery($id: BigInt!) {
                    catalogExperiment(id: $id) {
                        allSamples {
                            nodes {
                                sampleId
                                sampleName
                                sampleDataKey
                                sampleDataValue
                            }
                        }
                    }
                }
                """),
            {
                "id": self.id,
            },
        )["catalogExperiment"]["allSamples"]["nodes"]
        # todo(maximsmol): deal with nonexistent tables

        record_names: Dict[str, str] = {}
        record_values: Dict[str, Dict[str, RecordValue]] = {}

        for node in nodes:
            record_names[node["sampleId"]] = node["sampleName"]
            vals = record_values.setdefault(node["sampleId"], {})

            col = cols.get(node["sampleDataKey"])
            if col is None:
                continue

            # todo(maximsmol): in the future, allow storing or yielding values that failed to parse
            vals[col.key] = to_python_literal(
                node["sampleDataValue"], col.upstream_type["type"]
            )

        page: Dict[str, Record] = {}
        for id, values in record_values.items():
            for col in cols.values():
                if col.key in values:
                    continue

                if not col.upstream_type["allowEmpty"]:
                    values[col.key] = InvalidValue("")

            cur = Record(id)
            cur._cache.name = record_names[id]
            cur._cache.values = values
            cur._cache.types = col_types
            page[id] = cur

            if len(page) == page_size:
                yield page
                page = {}

        if len(page) > 0:
            yield page

    def update(self):
        return TableUpdater(self)


@dataclass(frozen=True)
class TableUpdate:
    table: Table

    # todo(maximsmol): switch to using l.DocumentNode
    @abstractmethod
    def get_document(self) -> str:
        raise NotImplementedError()


# todo(ayush): "DeleteRecordUpdate", "UpsertColumnUpdate", "DeleteColumnUpdate"
@dataclass(frozen=True)
class UpsertRecordUpdate(TableUpdate):
    name: str
    data: Dict[str, object]
    op_index: int

    @override
    def get_document(self) -> str:
        errors: Dict[str, str] = {}

        keys: List[str] = []
        registry_literal_strings: List[str] = []

        columns = self.table.get_columns()
        column_dict = {column.key: column.type for column in columns}

        for key, python_literal in self.data.items():
            try:
                registry_type = column_dict.get(key)
                registry_literal = to_registry_literal(
                    python_literal, registry_type["type"]
                )
            except RegistryTransformerException as e:
                errors[key] = f"unable to generate registry literal for {key}: {e}"
                continue

            keys.append(key)
            registry_literal_strings.append(json.dumps(registry_literal))

        arg_experiment_id = l.print_ast(
            u.ast_from_value(self.table.id, t.GraphQLString)
        )
        arg_name = l.print_ast(u.ast_from_value(self.name, t.GraphQLString))
        arg_keys = l.print_ast(u.ast_from_value(keys, t.GraphQLList(t.GraphQLString)))
        arg_data = l.print_ast(
            u.ast_from_value(registry_literal_strings, t.GraphQLList(t.GraphQLString))
        )

        return f"""
        m{self.op_index}: catalogUpsertSampleWithData(
            input: {{
                argExperimentId: {arg_experiment_id}
                argName: {arg_name}
                argKeys: {arg_keys}
                argData: {arg_data}
            }}
        ) {{
            clientMutationId
        }}
        """


@dataclass(frozen=True)
class TableUpdater:
    table: Table
    _updates: List[TableUpdate] = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, type, value, tb):
        if type is not None or value is not None or tb is not None:
            return False
        self.commit()

    def upsert_record(self, record_name: str, column_data: Dict[str, Any]):
        self._updates.append(
            UpsertRecordUpdate(
                self.table,
                record_name,
                column_data,
                len(self._updates),
            )
        )

    def commit(self):
        documents: List[str] = []

        while len(self._updates) > 0:
            update = self._updates.pop()
            documents.append(update.get_document())

        documents.reverse()

        batched_document = f"""
            mutation UpsertSampleWithData {{
                {"".join(documents)}
            }}
        """

        execute(gql.gql(batched_document))
