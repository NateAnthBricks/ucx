import datetime
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial

from databricks.labs.blueprint.installation import Installation
from databricks.labs.blueprint.parallel import Threads
from databricks.labs.lsql.backends import SqlBackend, StatementExecutionBackend
from databricks.sdk import WorkspaceClient

from databricks.labs.ucx.config import WorkspaceConfig
from databricks.labs.ucx.framework.crawlers import CrawlerBase
from databricks.labs.ucx.framework.utils import escape_sql_identifier
from databricks.labs.ucx.hive_metastore import TablesCrawler
from databricks.labs.ucx.hive_metastore.mapping import Rule, TableMapping
from databricks.labs.ucx.hive_metastore.tables import MigrationCount, Table, What

logger = logging.getLogger(__name__)


@dataclass
class MigrationStatus:
    src_schema: str
    src_table: str
    dst_catalog: str | None = None
    dst_schema: str | None = None
    dst_table: str | None = None
    update_ts: str | None = None


class TablesMigrate:
    def __init__(
        self,
        tables_crawler: TablesCrawler,
        ws: WorkspaceClient,
        backend: SqlBackend,
        table_mapping: TableMapping,
        migration_status_refresher,
    ):
        self._tc = tables_crawler
        self._backend = backend
        self._ws = ws
        self._tm = table_mapping
        self._migration_status_refresher = migration_status_refresher
        self._seen_tables: dict[str, str] = {}

    @classmethod
    def for_cli(cls, ws: WorkspaceClient, product='ucx'):
        installation = Installation.current(ws, product)
        config = installation.load(WorkspaceConfig)
        sql_backend = StatementExecutionBackend(ws, config.warehouse_id)
        table_crawler = TablesCrawler(sql_backend, config.inventory_database)
        table_mapping = TableMapping(installation, ws, sql_backend)
        migration_status_refresher = MigrationStatusRefresher(ws, sql_backend, config.inventory_database, table_crawler)
        return cls(table_crawler, ws, sql_backend, table_mapping, migration_status_refresher)

    def migrate_tables(self, *, what: What | None = None):
        self._init_seen_tables()
        tables_to_migrate = self._tm.get_tables_to_migrate(self._tc)
        tasks = []
        for table in tables_to_migrate:
            if not what or table.src.what == what:
                tasks.append(partial(self._migrate_table, table.src, table.rule))
        Threads.strict("migrate tables", tasks)

    def _migrate_table(self, src_table: Table, rule: Rule):
        if self._table_already_upgraded(rule.as_uc_table_key):
            logger.info(f"Table {src_table.key} already upgraded to {rule.as_uc_table_key}")
            return True
        if src_table.what == What.DBFS_ROOT_DELTA:
            return self._migrate_dbfs_root_table(src_table, rule)
        if src_table.what == What.EXTERNAL_SYNC:
            return self._migrate_external_table(src_table, rule)
        if src_table.what == What.VIEW:
            return self._migrate_view(src_table, rule)
        logger.info(f"Table {src_table.key} is not supported for migration")
        return True

    def _migrate_external_table(self, src_table: Table, rule: Rule):
        target_table_key = rule.as_uc_table_key
        table_migrate_sql = src_table.sql_migrate_external(target_table_key)
        logger.debug(f"Migrating external table {src_table.key} to using SQL query: {table_migrate_sql}")
        # have to wrap the fetch result with iter() for now, because StatementExecutionBackend returns iterator but RuntimeBackend returns list.
        sync_result = next(iter(self._backend.fetch(table_migrate_sql)))
        if sync_result.status_code != "SUCCESS":
            logger.warning(
                f"SYNC command failed to migrate {src_table.key} to {target_table_key}. Status code: {sync_result.status_code}. Description: {sync_result.description}"
            )
            return False
        self._backend.execute(src_table.sql_alter_from(rule.as_uc_table_key, self._ws.get_workspace_id()))
        return True

    def _migrate_dbfs_root_table(self, src_table: Table, rule: Rule):
        target_table_key = rule.as_uc_table_key
        table_migrate_sql = src_table.sql_migrate_dbfs(target_table_key)
        logger.debug(f"Migrating managed table {src_table.key} to using SQL query: {table_migrate_sql}")
        self._backend.execute(table_migrate_sql)
        self._backend.execute(src_table.sql_alter_to(rule.as_uc_table_key))
        self._backend.execute(src_table.sql_alter_from(rule.as_uc_table_key, self._ws.get_workspace_id()))
        return True

    def _migrate_view(self, src_table: Table, rule: Rule):
        target_table_key = rule.as_uc_table_key
        table_migrate_sql = src_table.sql_migrate_view(target_table_key)
        logger.debug(f"Migrating view {src_table.key} to using SQL query: {table_migrate_sql}")
        self._backend.execute(table_migrate_sql)
        self._backend.execute(src_table.sql_alter_to(rule.as_uc_table_key))
        self._backend.execute(src_table.sql_alter_from(rule.as_uc_table_key, self._ws.get_workspace_id()))
        return True

    def _table_already_upgraded(self, target) -> bool:
        return target in self._seen_tables

    def _get_tables_to_revert(self, schema: str | None = None, table: str | None = None) -> list[Table]:
        schema = schema.lower() if schema else None
        table = table.lower() if table else None
        upgraded_tables = []
        if table and not schema:
            logger.error("Cannot accept 'Table' parameter without 'Schema' parameter")

        for cur_table in self._tc.snapshot():
            if schema and cur_table.database != schema:
                continue
            if table and cur_table.name != table:
                continue
            if cur_table.key in self._seen_tables.values():
                upgraded_tables.append(cur_table)
        return upgraded_tables

    def revert_migrated_tables(
        self, schema: str | None = None, table: str | None = None, *, delete_managed: bool = False
    ):
        self._init_seen_tables()
        upgraded_tables = self._get_tables_to_revert(schema=schema, table=table)
        # reverses the _seen_tables dictionary to key by the source table
        reverse_seen = {v: k for (k, v) in self._seen_tables.items()}
        tasks = []
        for upgraded_table in upgraded_tables:
            if upgraded_table.kind == "VIEW" or upgraded_table.object_type == "EXTERNAL" or delete_managed:
                tasks.append(partial(self._revert_migrated_table, upgraded_table, reverse_seen[upgraded_table.key]))
                continue
            logger.info(
                f"Skipping {upgraded_table.object_type} Table {upgraded_table.database}.{upgraded_table.name} "
                f"upgraded_to {upgraded_table.upgraded_to}"
            )
        Threads.strict("revert migrated tables", tasks)

    def _revert_migrated_table(self, table: Table, target_table_key: str):
        logger.info(
            f"Reverting {table.object_type} table {table.database}.{table.name} upgraded_to {table.upgraded_to}"
        )
        self._backend.execute(table.sql_unset_upgraded_to())
        self._backend.execute(f"DROP {table.kind} IF EXISTS {target_table_key}")

    def _get_revert_count(self, schema: str | None = None, table: str | None = None) -> list[MigrationCount]:
        self._init_seen_tables()
        upgraded_tables = self._get_tables_to_revert(schema=schema, table=table)

        table_by_database = defaultdict(list)
        for cur_table in upgraded_tables:
            table_by_database[cur_table.database].append(cur_table)

        migration_list = []
        for cur_database, tables in table_by_database.items():
            what_count: dict[What, int] = {}
            for current_table in tables:
                if current_table.upgraded_to is not None:
                    count = what_count.get(current_table.what, 0)
                    what_count[current_table.what] = count + 1
            migration_list.append(MigrationCount(database=cur_database, what_count=what_count))
        return migration_list

    def is_upgraded(self, schema: str, table: str) -> bool:
        return self._migration_status_refresher.is_upgraded(schema, table)

    def print_revert_report(self, *, delete_managed: bool) -> bool | None:
        migrated_count = self._get_revert_count()
        if not migrated_count:
            logger.info("No migrated tables were found.")
            return False
        print("The following is the count of migrated tables and views found in scope:")
        table_header = "Database            |"
        headers = 1
        for what in list(What):
            headers = max(headers, len(what.name.split("_")))
            table_header += f" {what.name.split('_')[0]:<10} |"
        print(table_header)
        # Split the header so _ separated what names are splitted into multiple lines
        for header in range(1, headers):
            table_sub_header = "                    |"
            for what in list(What):
                if len(what.name.split("_")) - 1 < header:
                    table_sub_header += f"{' '*12}|"
                    continue
                table_sub_header += f" {what.name.split('_')[header]:<10} |"
            print(table_sub_header)
        separator = "=" * (22 + 13 * len(What))
        print(separator)
        for count in migrated_count:
            table_row = f"{count.database:<20}|"
            for what in list(What):
                table_row += f" {count.what_count.get(what, 0):10} |"
            print(table_row)
        print(separator)
        print("The following actions will be performed")
        print("- Migrated External Tables and Views (targets) will be deleted")
        if delete_managed:
            print("- Migrated DBFS Root Tables will be deleted")
        else:
            print("- Migrated DBFS Root Tables will be left intact")
            print("To revert and delete Migrated Tables, add --delete_managed true flag to the command")
        return True

    def _init_seen_tables(self):
        self._seen_tables = self._migration_status_refresher.get_seen_tables()


class MigrationStatusRefresher(CrawlerBase[MigrationStatus]):
    def __init__(self, ws: WorkspaceClient, sbe: SqlBackend, schema, table_crawler: TablesCrawler):
        super().__init__(sbe, "hive_metastore", schema, "migration_status", MigrationStatus)
        self._ws = ws
        self._table_crawler = table_crawler

    def snapshot(self) -> Iterable[MigrationStatus]:
        return self._snapshot(self._try_fetch, self._crawl)

    def get_seen_tables(self) -> dict[str, str]:
        seen_tables: dict[str, str] = {}
        for schema in self._iter_schemas():
            for table in self._ws.tables.list(catalog_name=schema.catalog_name, schema_name=schema.name):
                if not table.properties:
                    continue
                if "upgraded_from" not in table.properties:
                    continue
                if not table.full_name:
                    logger.warning(f"The table {table.name} in {schema.name} has no full name")
                    continue
                seen_tables[table.full_name.lower()] = table.properties["upgraded_from"].lower()
        return seen_tables

    def is_upgraded(self, schema: str, table: str) -> bool:
        result = self._backend.fetch(f"SHOW TBLPROPERTIES {escape_sql_identifier(schema+'.'+table)}")
        for value in result:
            if value["key"] == "upgraded_to":
                logger.info(f"{schema}.{table} is set as upgraded")
                return True
        logger.info(f"{schema}.{table} is set as not upgraded")
        return False

    def _crawl(self) -> Iterable[MigrationStatus]:
        all_tables = self._table_crawler.snapshot()
        reverse_seen = {v: k for k, v in self.get_seen_tables().items()}
        timestamp = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for table in all_tables:
            table_migration_status = MigrationStatus(
                src_schema=table.database,
                src_table=table.name,
                update_ts=str(timestamp),
            )
            if table.key in reverse_seen and self.is_upgraded(table.database, table.name):
                target_table = reverse_seen[table.key]
                if len(target_table.split(".")) == 3:
                    table_migration_status.dst_catalog = target_table.split(".")[0]
                    table_migration_status.dst_schema = target_table.split(".")[1]
                    table_migration_status.dst_table = target_table.split(".")[2]
            yield table_migration_status

    def _try_fetch(self) -> Iterable[MigrationStatus]:
        for row in self._fetch(f"SELECT * FROM {self._schema}.{self._table}"):
            yield MigrationStatus(*row)

    def _iter_schemas(self):
        for catalog in self._ws.catalogs.list():
            yield from self._ws.schemas.list(catalog_name=catalog.name)
