from typing import Optional

from latch.gql.execute import execute
from latch.registry.filter import NumberFilter, StringFilter
from latch.registry.table import Table


class Project:
    def __init__(self, id: str):
        self.id = id

        self._display_name = None

    def get_display_name(self):
        if self._display_name is not None:
            return self._display_name

        self._display_name = execute(
            document="""
                query projectNameQuery ($argProjectId: BigInt!) {
                    catalogProject(id: $argProjectId) {
                        id
                        displayName
                    }
                }
            """,
            variables={"argProjectId": self.id},
        )["catalogProject"]["displayName"]

        return self._display_name

    def list_tables(
        self,
        id: Optional[NumberFilter] = None,
        display_name: Optional[StringFilter] = None,
    ):
        filters = []
        if id is not None:
            filters.append(f"id: {id}")
        if display_name is not None:
            filters.append(f"displayName: {display_name}")
        filter_str = "\n".join(filters)

        if len(filter_str) == 0:
            query = f"""
                query ExperimentsQuery ($argProjectId: BigInt!) {{
                    catalogExperiments (
                        condition: {{
                            projectId: $argProjectId
                            removed: false
                        }}
                    ) {{
                        nodes {{
                            id
                            displayName
                        }}
                    }}
                }}
            """
        else:
            query = f"""
                query ExperimentsQuery ($argProjectId: BigInt!) {{
                    catalogExperiments (
                        condition: {{
                            projectId: $argProjectId
                            removed: false
                        }}
                        filter: {{
                            {filter_str}
                        }}
                    ) {{
                        nodes {{
                            id
                            displayName
                        }}
                    }}
                }}
            """

        data = execute(query, {"argProjectId": self.id})

        return [Table(node["id"]) for node in data["catalogExperiments"]["nodes"]]

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        if self._display_name is not None:
            return f"Project(display_name={self._display_name})"
        return f"Project(id={self.id})"
