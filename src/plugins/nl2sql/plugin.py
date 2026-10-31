import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Annotated

from .nl2sql_types import (
    DataSourceItem,
    DataSourcesList,
    ExecuteQueryResult,
    MeasureItem,
    MeasuresList,
    QueryItem,
    QueriesRetrievalResult,
    SchemaInfo,
    TableItem,
    TableRetrievalItem,
    TablesList,
    TablesRetrievalResult,
    ValidateSQLQueryResult,
)
from .sql_validation import validate_single_read_only_select

from connectors import (
    get_cosmosdb_client,
    get_genai_client,
    get_search_client,
    SemanticModelClient,
    SQLDBClient,
    SQLEndpointClient,
)
from connectors.types import (
    SemanticModelConfig,
    SQLDatabaseConfig,
    SQLEndpointConfig,
)

from dependencies import get_config

class NL2SQLPlugin:
    def __init__(self, *, config=None, source_registry=None, search=None, model=None,
                 query_timeout_seconds=None, max_rows=None):
        # Native applications supply capability-owned clients. Existing consumers
        # retain their constructor contract; no runtime-global Cosmos is needed.
        self.cosmos = source_registry if source_registry is not None else get_cosmosdb_client()
        self.search = search if search is not None else get_search_client()
        self.model = model
        self.query_timeout_seconds = query_timeout_seconds
        self.max_rows = max_rows
        if query_timeout_seconds is not None and not 1 <= query_timeout_seconds <= 300:
            raise ValueError("NL2SQL query timeout must be between 1 and 300 seconds.")
        if max_rows is not None and not 1 <= max_rows <= 10000:
            raise ValueError("NL2SQL row limit must be between 1 and 10000.")
        cfg = config if config is not None else get_config()
        self.container_name = cfg.get("DATASOURCES_CONTAINER", "datasources")
        self.tables_index = cfg.get("SEARCH_TABLES_INDEX_NAME", "nl2sql-tables")
        self.measures_index = cfg.get("SEARCH_MEASURES_INDEX_NAME", "nl2sql-measures")
        self.queries_index = cfg.get("SEARCH_QUERIES_INDEX_NAME", "nl2sql-queries")
        self.search_approach = cfg.get("SEARCH_APPROACH", "hybrid").lower()
        self.use_semantic = cfg.get("SEARCH_USE_SEMANTIC", "false") == "true"
        self.semantic_config = cfg.get("SEARCH_SEMANTIC_SEARCH_CONFIG", "my-semantic-config")
        self.tables_top_k = int(cfg.get("SEARCH_TABLES_INDEX_TOP_K", 10))
        self.queries_top_k = int(cfg.get("SEARCH_QUERIES_INDEX_TOP_K", 3))

    async def _perform_search(self, body: Dict[str, Any], search_index: str) -> Dict[str, Any]:
        return await self.search.search(index_name=search_index, body=body)

    async def get_all_datasources_info(self) -> DataSourcesList:
        documents = await self.cosmos.list_documents(self.container_name)
        datasources = [
            DataSourceItem(
                name=doc.get("id", ""),
                description=doc.get("description", ""),
                type=doc.get("type", ""),
            )
            for doc in documents
        ]
        return DataSourcesList(datasources=datasources)

    async def get_all_tables_info(
        self,
        datasource: Annotated[str, "Name of the target datasource"]
    ) -> TablesList:
        search_index = self.tables_index
        safe_ds = datasource.replace("'", "''")
        body = {
            "search": "*",
            "filter": f"datasource eq '{safe_ds}'",
            "select": "table, description, datasource",
            "top": 1000
        }

        logging.info(f"[tables] Querying tables for datasource '{datasource}'")
        tables_info: List[TableItem] = []
        error: Optional[str] = None

        try:
            start = time.time()
            result = await self._perform_search(body, search_index)
            logging.info(f"[tables] completed in {round(time.time()-start,2)}s")
            for doc in result.get("value", []):
                tables_info.append(
                    TableItem(
                        table=doc.get("table", ""),
                        description=doc.get("description", ""),
                        datasource=doc.get("datasource", ""),
                    )
                )
        except Exception as ex:
            error = type(ex).__name__
            logging.error(f"[tables] error: {error}")

        if not tables_info:
            return TablesList(
                tables=[],
                error=f"No datasource named '{datasource}'. {error or ''}".strip()
            )

        return TablesList(tables=tables_info, error=error)

    async def get_schema_info(
        self,
        datasource: Annotated[str, "Target datasource"],
        table_name: Annotated[str, "Target table"]
    ) -> SchemaInfo:
        search_index = self.tables_index
        safe_ds = datasource.replace("'", "''")
        safe_table = table_name.replace("'", "''")
        body = {
            "search": "*",
            "filter": f"datasource eq '{safe_ds}' and table eq '{safe_table}'",
            "select": "table, description, datasource, columns",
            "top": 1
        }

        logging.info(f"[tables] Querying schema for '{table_name}' in '{datasource}'")
        try:
            start = time.time()
            result = await self._perform_search(body, search_index)
            logging.info(f"[tables] completed in {round(time.time()-start,2)}s")
            docs = result.get("value", [])
            if not docs:
                return SchemaInfo(
                    datasource=datasource,
                    table=table_name,
                    error="Table not found.",
                    columns=None
                )
            doc = docs[0]
            cols = {}
            for c in doc.get("columns", []):
                name = c.get("name")
                if name:
                    cols[name] = c.get("description", "")
            return SchemaInfo(
                datasource=datasource,
                table=doc.get("table", table_name),
                description=doc.get("description", ""),
                columns=cols
            )
        except Exception as ex:
            msg = type(ex).__name__
            logging.error(f"[tables] schema error: {msg}")
            return SchemaInfo(
                datasource=datasource,
                table=table_name,
                error=msg,
                columns=None
            )

    async def tables_retrieval(
        self,
        input: Annotated[str, "Optimized retrieval query"],
    datasource: Annotated[str, "Target datasource"] = None
    ) -> TablesRetrievalResult:
        idx = self.tables_index
        approach = self.search_approach
        k = self.tables_top_k
        use_semantic = self.use_semantic
        sem_cfg = self.semantic_config

        aoai = self.model if self.model is not None else get_genai_client()
        logging.info("[tables] generating embeddings")
        embeddings = await aoai.get_embeddings(input)

        body: Dict[str, Any] = {"select": "table, description", "top": k}
        if datasource:
            safe_ds = datasource.replace("'", "''")
            body["filter"] = f"datasource eq '{safe_ds}'"
        if approach in ("term", "hybrid"):
            body["search"] = input
        if approach in ("vector", "hybrid"):
            body.setdefault("vectorQueries", []).append({
                "kind": "vector",
                "vector": embeddings,
                "fields": "contentVector",
                "k": k
            })
        if use_semantic and approach != "vector":
            body["queryType"] = "semantic"
            body["semanticConfiguration"] = sem_cfg

        logging.info("[tables] retrieving tables")
        results: List[TableRetrievalItem] = []
        err: Optional[str] = None
        try:
            start = time.time()
            res = await self._perform_search(body, idx)
            logging.info(f"[tables] done in {round(time.time()-start,2)}s")
            for d in res.get("value", []):
                results.append(TableRetrievalItem(
                    table=d.get("table",""),
                    description=d.get("description",""),
                    datasource=d.get("datasource",None)
                ))
        except Exception as ex:
            err = type(ex).__name__
            logging.error(f"[tables] retrieval error: {err}")

        return TablesRetrievalResult(tables=results, error=err)

    async def measures_retrieval(
        self,
        datasource: Annotated[str, "Name of the target datasource"]
    ) -> MeasuresList:
        idx = self.measures_index
        safe_ds = datasource.replace("'", "''")
        body = {
            "search": "*",
            "filter": f"datasource eq '{safe_ds}'",
            "select": "name, description, datasource, type, source_table, data_type, source_model",
            "top": 1000
        }

        logging.info(f"[measures] querying for datasource '{datasource}'")
        measures: List[MeasureItem] = []
        err: Optional[str] = None
        try:
            start = time.time()
            res = await self._perform_search(body, idx)
            logging.info(f"[measures] done in {round(time.time()-start,2)}s")
            for d in res.get("value", []):
                measures.append(MeasureItem(
                    name=d.get("name",""),
                    description=d.get("description",""),
                    datasource=d.get("datasource",""),
                    type=d.get("type", "external"),
                    source_table=d.get("source_table", None),
                    data_type=d.get("data_type", None),
                    source_model=d.get("source_model", None)
                ))
        except Exception as ex:
            err = type(ex).__name__
            logging.error(f"[measures] error: {err}")

        if not measures:
            return MeasuresList(measures=[], error=f"No measures for '{datasource}'. {err or ''}".strip())

        return MeasuresList(measures=measures, error=err)

    async def queries_retrieval(
        self,
        input: Annotated[str, "The user ask"],
        datasource: Annotated[str, "Target datasource name"] = None,
    ) -> QueriesRetrievalResult:
        VECTOR, TERM, HYBRID = "vector", "term", "hybrid"
        idx = self.queries_index
        approach = self.search_approach
        top_k = self.queries_top_k
        use_sem = self.use_semantic
        sem_cfg = self.semantic_config

        aoai = self.model if self.model is not None else get_genai_client()
        logging.info("[queries] generating embeddings")
        embeddings = await aoai.get_embeddings(input)

        body: Dict[str, Any] = {"select": "question, query, reasoning", "top": top_k}
        if datasource:
            safe_ds = datasource.replace("'", "''")
            body["filter"] = f"datasource eq '{safe_ds}'"
        if approach in (TERM, HYBRID):
            body["search"] = input
        if approach in (VECTOR, HYBRID):
            body.setdefault("vectorQueries", []).append({
                "kind": "vector",
                "vector": embeddings,
                "fields": "contentVector",
                "k": top_k
            })
        if use_sem and approach != VECTOR:
            body["queryType"] = "semantic"
            body["semanticConfiguration"] = sem_cfg

        logging.info("[queries] querying index")
        results = []
        err: Optional[str] = None
        try:
            start = time.time()
            res = await self._perform_search(body, idx)
            logging.info(f"[queries] done in {round(time.time()-start,2)}s")
            for d in res.get("value", []):
                results.append(QueryItem(
                    question=d.get("question",""),
                    query=d.get("query",""),
                    reasoning=d.get("reasoning","")
                ))
        except Exception as ex:
            err = type(ex).__name__
            logging.error(f"[queries] error: {err}")

        return QueriesRetrievalResult(queries=results, error=err)

    async def validate_sql_query(
        self,
        query: Annotated[str, "SQL Query"]
    ) -> ValidateSQLQueryResult:
        validation = validate_single_read_only_select(query)
        return ValidateSQLQueryResult(is_valid=validation.is_valid, error=validation.error)

    async def execute_dax_query(
        self,
        datasource: Annotated[str, "Target datasource"],
        query: Annotated[str, "DAX Query"],
        access_token: Annotated[str, "User Access Token"]
    ) -> ExecuteQueryResult:
        try:
            cosmosdb = self.cosmos
            datasource_config = await cosmosdb.get_document(self.container_name, datasource)
            if not datasource_config or datasource_config.get("type") != "semantic_model":
                return ExecuteQueryResult(error=f"{datasource} datasource configuration not found or invalid for Semantic Model.")

            semantic_model_config = SemanticModelConfig(
                id=datasource_config.get("id"),
                description=datasource_config.get("description"),
                type=datasource_config.get("type"),
                organization=datasource_config.get("organization"),
                dataset=datasource_config.get("dataset"),
                workspace=datasource_config.get("workspace"),
                tenant_id=datasource_config.get("tenant_id"),
                client_id=datasource_config.get("client_id")
            )
            semantic_client = SemanticModelClient(semantic_model_config)
            results = await semantic_client.execute_restapi_dax_query(dax_query=query, user_token=access_token)
            return ExecuteQueryResult(results=results)
        except Exception as e:
            logging.error("[dax] query failed (%s)", type(e).__name__)
            return ExecuteQueryResult(error=type(e).__name__)

    async def execute_sql_query(
        self,
        datasource: Annotated[str, "Target datasource name"],
        query: Annotated[str, "SQL Query"]
    ) -> ExecuteQueryResult:
        # log entry and parameters
        logging.info("execute_sql_query called (datasource=%s)", datasource)
        connection = None
        cursor = None
        try:
            validation = validate_single_read_only_select(query)
            if not validation.is_valid:
                logging.error(f"Rejected SQL query: {validation.error}")
                return ExecuteQueryResult(error=validation.error)

            cosmosdb = self.cosmos
            datasource_config = await cosmosdb.get_document(self.container_name, datasource)
            logging.debug("Datasource configuration fetched (present=%s)", datasource_config is not None)

            if not datasource_config:
                logging.error(f"Datasource '{datasource}' configuration not found.")
                return ExecuteQueryResult(error=f"{datasource} datasource configuration not found.")

            datasource_type = datasource_config.get("type")
            # select client based on type
            if datasource_type == "sql_endpoint":
                logging.info("Initializing SQLEndpointClient")
                sql_endpoint_config = SQLEndpointConfig(
                    id=datasource_config.get("id"),
                    description=datasource_config.get("description"),
                    type=datasource_config.get("type"),
                    organization=datasource_config.get("organization"),
                    server=datasource_config.get("server"),
                    database=datasource_config.get("database"),
                    tenant_id=datasource_config.get("tenant_id"),
                    client_id=datasource_config.get("client_id")
                )
                sql_client = SQLEndpointClient(sql_endpoint_config)
            elif datasource_type == "sql_database":
                logging.info("Initializing SQLDBClient")
                sql_database_config = SQLDatabaseConfig(
                    id=datasource_config.get("id"),
                    description=datasource_config.get("description"),
                    type=datasource_config.get("type"),
                    server=datasource_config.get("server"),
                    database=datasource_config.get("database"),
                    uid=datasource_config.get("uid", None)
                )
                sql_client = SQLDBClient(sql_database_config)
            else:
                logging.error(f"Unsupported datasource type: {datasource_type}")
                return ExecuteQueryResult(error="Datasource type not supported for SQL queries.")

            if self.query_timeout_seconds is not None or self.max_rows is not None:
                return await self._execute_bounded_sql(sql_client, query)

            logging.info("Creating SQL connection")
            connection = await sql_client.create_connection()
            cursor = connection.cursor()

            logging.info("Executing SQL query")
            cursor.execute(query)
            logging.info("Fetching query results")
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            if self.max_rows is not None and len(rows) > self.max_rows:
                return ExecuteQueryResult(error="Query exceeded the configured row limit.")
            logging.info(f"Query returned {len(rows)} rows")

            results = [dict(zip(columns, row)) for row in rows]
            return ExecuteQueryResult(results=results)

        except Exception as e:
            logging.error("execute_sql_query error (%s)", type(e).__name__)
            return ExecuteQueryResult(error=type(e).__name__)
        finally:
            # Closing an ODBC connection does not close the cursor explicitly.
            # Attempt both releases, even when the first fails, without replacing
            # the primary typed result or a propagating cancellation.
            for resource_name, resource in (("cursor", cursor), ("connection", connection)):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as cleanup_error:
                        logging.warning(
                            "SQL %s cleanup failed (%s)",
                            resource_name,
                            type(cleanup_error).__name__,
                        )

    async def _execute_bounded_sql(self, client: Any, query: str) -> ExecuteQueryResult:
        async def operation() -> ExecuteQueryResult:
            connection = await client.create_connection()
            # One thread owns execute, fetch and cleanup. Cancellation must never
            # close an ODBC handle while that same handle is executing elsewhere.
            return await asyncio.to_thread(self._bounded_query, connection, query)

        task = asyncio.create_task(operation())
        cancellation = None
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as cancelled:
                if task.cancelled():
                    raise
                cancellation = cancelled
            except Exception as error:
                if cancellation is None:
                    raise
                logging.warning("Cancelled SQL operation failed (%s)", type(error).__name__)
                raise cancellation from None
        if cancellation is not None:
            raise cancellation
        return result

    def _bounded_query(self, connection: Any, query: str) -> ExecuteQueryResult:
        cursor = None
        try:
            if self.query_timeout_seconds is not None:
                connection.timeout = self.query_timeout_seconds
            cursor = connection.cursor()
            cursor.execute(query)
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchmany(self.max_rows + 1) if self.max_rows is not None else cursor.fetchall()
            if self.max_rows is not None and len(rows) > self.max_rows:
                return ExecuteQueryResult(error="Query exceeded the configured row limit.")
            return ExecuteQueryResult(results=[dict(zip(columns, row)) for row in rows])
        finally:
            for resource_name, resource in (("cursor", cursor), ("connection", connection)):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as cleanup_error:
                        logging.warning("SQL %s cleanup failed (%s)", resource_name, type(cleanup_error).__name__)