import io
import json
import subprocess
from unittest.mock import create_autospec, patch

import pytest
import yaml
from databricks.labs.blueprint.installation import Installation
from databricks.labs.blueprint.tui import MockPrompts
from databricks.sdk import AccountClient, WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service import iam, sql
from databricks.sdk.service.compute import ClusterDetails, ClusterSource
from databricks.sdk.service.workspace import ObjectInfo

from databricks.labs.ucx.assessment.aws import AWSResources
from databricks.labs.ucx.aws.access import AWSResourcePermissions
from databricks.labs.ucx.azure.access import AzureResourcePermissions
from databricks.labs.ucx.cli import (
    alias,
    cluster_remap,
    create_account_groups,
    create_catalogs_schemas,
    create_table_mapping,
    create_uber_principal,
    ensure_assessment_run,
    installations,
    manual_workspace_info,
    migrate_credentials,
    migrate_locations,
    move,
    open_remote_config,
    principal_prefix_access,
    repair_run,
    revert_cluster_remap,
    revert_migrated_tables,
    skip,
    sync_workspace_info,
    validate_external_locations,
    validate_groups_membership,
    workflows,
)


@pytest.fixture
def ws():
    state = {
        "/Users/foo/.ucx/config.yml": yaml.dump(
            {
                'version': 2,
                'inventory_database': 'ucx',
                'warehouse_id': 'test',
                'connect': {
                    'host': 'foo',
                    'token': 'bar',
                },
            }
        ),
        '/Users/foo/.ucx/state.json': json.dumps({'resources': {'jobs': {'assessment': '123'}}}),
        "/Users/foo/.ucx/uc_roles_access.csv": "role_arn,resource_type,privilege,resource_path\n"
        "arn:aws:iam::123456789012:role/role_name,s3,READ_FILES,s3://labsawsbucket/",
        "/Users/foo/.ucx/azure_storage_account_info.csv": "prefix,client_id,principal,privilege,type,directory_id\ntest,test,test,test,Application,test",
        "/Users/foo/.ucx/mapping.csv": "workspace_name,catalog_name,src_schema,dst_schema,src_table,dst_table\ntest,test,test,test,test,test",
    }

    def download(path: str) -> io.StringIO | io.BytesIO:
        if path not in state:
            raise NotFound(path)
        if ".csv" in path:
            return io.BytesIO(state[path].encode('utf-8'))
        return io.StringIO(state[path])

    workspace_client = create_autospec(WorkspaceClient)
    workspace_client.get_workspace_id.return_value = 123
    workspace_client.config.host = 'https://localhost'
    workspace_client.current_user.me().user_name = "foo"
    workspace_client.workspace.download = download
    workspace_client.statement_execution.execute_statement.return_value = sql.ExecuteStatementResponse(
        status=sql.StatementStatus(state=sql.StatementState.SUCCEEDED),
        manifest=sql.ResultManifest(schema=sql.ResultSchema()),
        statement_id='123',
    )
    return workspace_client


def test_workflow(ws, caplog):
    workflows(ws)
    assert "Fetching deployed jobs..." in caplog.messages
    ws.jobs.list_runs.assert_called_once()


def test_open_remote_config(ws):
    with patch("webbrowser.open") as mock_webbrowser_open:
        open_remote_config(ws)
        mock_webbrowser_open.assert_called_with('https://localhost/#workspace/Users/foo/.ucx/config.yml')


def test_installations(ws, capsys):
    ws.users.list.return_value = [iam.User(user_name='foo')]
    installations(ws)
    assert '{"database": "ucx", "path": "/Users/foo/.ucx", "warehouse_id": "test"}' in capsys.readouterr().out


def test_skip_with_table(ws):
    skip(ws, "schema", "table")

    ws.statement_execution.execute_statement.assert_called_with(
        warehouse_id='test',
        statement="ALTER TABLE schema.table SET TBLPROPERTIES('databricks.labs.ucx.skip' = true)",
        byte_limit=None,
        catalog=None,
        schema=None,
        disposition=None,
        format=sql.Format.JSON_ARRAY,
        wait_timeout=None,
    )


def test_skip_with_schema(ws):
    skip(ws, "schema", None)

    ws.statement_execution.execute_statement.assert_called_with(
        warehouse_id='test',
        statement="ALTER SCHEMA schema SET DBPROPERTIES('databricks.labs.ucx.skip' = true)",
        byte_limit=None,
        catalog=None,
        schema=None,
        disposition=None,
        format=sql.Format.JSON_ARRAY,
        wait_timeout=None,
    )


def test_skip_no_schema(ws, caplog):
    skip(ws, schema=None, table="table")

    assert '--schema is a required parameter.' in caplog.messages


def test_sync_workspace_info():
    a = create_autospec(AccountClient)
    sync_workspace_info(a)
    a.workspaces.list.assert_called()


def test_create_account_groups():
    a = create_autospec(AccountClient)
    w = create_autospec(WorkspaceClient)
    a.get_workspace_client.return_value = w
    w.get_workspace_id.return_value = None
    prompts = MockPrompts({})
    create_account_groups(a, prompts, new_workspace_client=lambda: w)
    a.groups.list.assert_called_with(attributes="id")


def test_manual_workspace_info(ws):
    prompts = MockPrompts({'Workspace name for 123': 'abc', 'Next workspace id': ''})
    manual_workspace_info(ws, prompts)


def test_create_table_mapping(ws):
    with pytest.raises(ValueError, match='databricks labs ucx sync-workspace-info'):
        create_table_mapping(ws)


def test_validate_external_locations(ws):
    validate_external_locations(ws, MockPrompts({}))

    ws.statement_execution.execute_statement.assert_called()


def test_ensure_assessment_run(ws):
    ensure_assessment_run(ws)

    ws.jobs.list_runs.assert_called_once()


def test_repair_run(ws):
    repair_run(ws, "assessment")

    ws.jobs.list_runs.assert_called_once()


def test_no_step_in_repair_run(ws):
    with pytest.raises(KeyError):
        repair_run(ws, "")


def test_revert_migrated_tables(ws, caplog):
    # test with no schema and no table, user confirm to not retry
    prompts = MockPrompts({'.*': 'no'})
    assert revert_migrated_tables(ws, prompts, schema=None, table=None) is None

    # test with no schema and no table, user confirm to retry, but no ucx installation found
    prompts = MockPrompts({'.*': 'yes'})
    assert revert_migrated_tables(ws, prompts, schema=None, table=None) is None
    assert 'No migrated tables were found.' in caplog.messages


def test_move_no_catalog(ws, caplog):
    prompts = MockPrompts({})
    move(ws, prompts, "", "", "", "", "")

    assert 'Please enter from_catalog and to_catalog details' in caplog.messages


def test_move_same_schema(ws, caplog):
    prompts = MockPrompts({})
    move(ws, prompts, "SrcCat", "SrcS", "*", "SrcCat", "SrcS")

    assert 'please select a different schema or catalog to migrate to' in caplog.messages


def test_move_no_schema(ws, caplog):
    prompts = MockPrompts({})
    move(ws, prompts, "SrcCat", "", "*", "TgtCat", "")

    assert (
        'Please enter from_schema, to_schema and from_table (enter * for migrating all tables) details.'
        in caplog.messages
    )


def test_move(ws):
    prompts = MockPrompts({'.*': 'yes'})
    move(ws, prompts, "SrcC", "SrcS", "*", "TgtC", "ToS")

    ws.tables.list.assert_called_once()


def test_alias_no_catalog(ws, caplog):
    alias(ws, "", "", "", "", "")

    assert "Please enter from_catalog and to_catalog details" in caplog.messages


def test_alias_same_schema(ws, caplog):
    alias(ws, "SrcCat", "SrcS", "*", "SrcCat", "SrcS")

    assert 'please select a different schema or catalog to migrate to' in caplog.messages


def test_alias_no_schema(ws, caplog):
    alias(ws, "SrcCat", "", "*", "TgtCat", "")

    assert (
        'Please enter from_schema, to_schema and from_table (enter * for migrating all tables) details.'
        in caplog.messages
    )


def test_alias(ws):
    alias(ws, "SrcC", "SrcS", "*", "TgtC", "ToS")

    ws.tables.list.assert_called_once()


def test_save_storage_and_principal_azure_no_azure_cli(ws, caplog):
    ws.config.auth_type = "azure_clis"
    ws.config.is_azure = True
    prompts = MockPrompts({})
    principal_prefix_access(ws, prompts, "")

    assert 'In order to obtain AAD token, Please run azure cli to authenticate.' in caplog.messages


def test_save_storage_and_principal_azure_no_subscription_id(ws, caplog):
    ws.config.auth_type = "azure-cli"
    ws.config.is_azure = True

    prompts = MockPrompts({})
    principal_prefix_access(ws, prompts)

    assert "Please enter subscription id to scan storage accounts in." in caplog.messages


def test_save_storage_and_principal_azure(ws, caplog):
    ws.config.auth_type = "azure-cli"
    ws.config.is_azure = True
    prompts = MockPrompts({})
    azure_resource_permissions = create_autospec(AzureResourcePermissions)
    principal_prefix_access(ws, prompts, subscription_id="test", azure_resource_permissions=azure_resource_permissions)
    azure_resource_permissions.save_spn_permissions.assert_called_once()


def test_validate_groups_membership(ws):
    validate_groups_membership(ws)
    ws.groups.list.assert_called()


def test_save_storage_and_principal_aws_no_profile(ws, caplog, mocker):
    mocker.patch("shutil.which", return_value="/path/aws")
    ws.config.is_azure = False
    ws.config.is_aws = True
    prompts = MockPrompts({})
    principal_prefix_access(ws, prompts)
    assert any({"AWS Profile is not specified." in message for message in caplog.messages})


def test_save_storage_and_principal_aws_no_connection(ws, mocker):
    mocker.patch("shutil.which", return_value="/path/aws")
    pop = create_autospec(subprocess.Popen)
    ws.config.is_azure = False
    ws.config.is_aws = True
    pop.communicate.return_value = (bytes("message", "utf-8"), bytes("error", "utf-8"))
    pop.returncode = 127
    mocker.patch("subprocess.Popen.__init__", return_value=None)
    mocker.patch("subprocess.Popen.__enter__", return_value=pop)
    mocker.patch("subprocess.Popen.__exit__", return_value=None)
    prompts = MockPrompts({})

    with pytest.raises(ResourceWarning, match="AWS CLI is not configured properly."):
        principal_prefix_access(ws, prompts, aws_profile="profile")


def test_save_storage_and_principal_aws_no_cli(ws, mocker, caplog):
    mocker.patch("shutil.which", return_value=None)
    ws.config.is_azure = False
    ws.config.is_aws = True
    prompts = MockPrompts({})
    principal_prefix_access(ws, prompts, aws_profile="profile")
    assert any({"Couldn't find AWS" in message for message in caplog.messages})


def test_save_storage_and_principal_aws(ws, mocker, caplog):
    mocker.patch("shutil.which", return_value=True)
    ws.config.is_azure = False
    ws.config.is_aws = True
    aws_resource_permissions = create_autospec(AWSResourcePermissions)
    prompts = MockPrompts({})
    principal_prefix_access(ws, prompts, aws_profile="profile", aws_resource_permissions=aws_resource_permissions)
    aws_resource_permissions.save_instance_profile_permissions.assert_called_once()


def test_save_storage_and_principal_gcp(ws, caplog):
    ws.config.is_azure = False
    ws.config.is_aws = False
    ws.config.is_gcp = True
    prompts = MockPrompts({})
    principal_prefix_access(ws, prompts)
    assert "This cmd is only supported for azure and aws workspaces" in caplog.messages


def test_migrate_credentials_azure(ws):
    ws.config.is_azure = True
    ws.workspace.upload.return_value = "test"
    prompts = MockPrompts({'.*': 'yes'})
    migrate_credentials(ws, prompts)
    ws.storage_credentials.list.assert_called()


def test_migrate_credentials_aws(ws, mocker):
    mocker.patch("shutil.which", return_value=True)
    ws.config.is_azure = False
    ws.config.is_aws = True
    ws.config.is_gcp = False
    aws_resources = create_autospec(AWSResources)
    aws_resources.validate_connection.return_value = {"Account": "123456789012"}
    prompts = MockPrompts({'.*': 'yes'})
    migrate_credentials(ws, prompts, aws_profile="profile", aws_resources=aws_resources)
    ws.storage_credentials.list.assert_called()
    aws_resources.update_uc_trust_role.assert_called_once()


def test_migrate_credentials_aws_no_profile(ws, caplog, mocker):
    mocker.patch("shutil.which", return_value="/path/aws")
    ws.config.is_azure = False
    ws.config.is_aws = True
    prompts = MockPrompts({})
    migrate_credentials(ws, prompts)
    assert (
        "AWS Profile is not specified. Use the environment variable [AWS_DEFAULT_PROFILE] or use the "
        "'--aws-profile=[profile-name]' parameter." in caplog.messages
    )


def test_create_master_principal_not_azure(ws):
    ws.config.is_azure = False
    prompts = MockPrompts({})
    create_uber_principal(ws, prompts, subscription_id="")
    ws.workspace.get_status.assert_not_called()


def test_create_master_principal_no_azure_cli(ws):
    ws.config.auth_type = "azure_clis"
    ws.config.is_azure = True
    prompts = MockPrompts({})
    create_uber_principal(ws, prompts, subscription_id="")
    ws.workspace.get_status.assert_not_called()


def test_create_master_principal_no_subscription(ws):
    ws.config.auth_type = "azure-cli"
    ws.config.is_azure = True
    prompts = MockPrompts({})
    create_uber_principal(ws, prompts, subscription_id="")
    ws.workspace.get_status.assert_not_called()


def test_create_uber_principal(ws):
    ws.config.auth_type = "azure-cli"
    ws.config.is_azure = True
    prompts = MockPrompts({})
    with pytest.raises(ValueError):
        create_uber_principal(ws, prompts, subscription_id="12")


def test_migrate_locations_azure(ws):
    ws.config.is_azure = True
    ws.config.is_aws = False
    ws.config.is_gcp = False
    migrate_locations(ws)
    ws.external_locations.list.assert_called()


def test_migrate_locations_aws(ws, caplog, mocker):
    mocker.patch("shutil.which", return_value=True)
    ws.config.is_azure = False
    ws.config.is_aws = True
    ws.config.is_gcp = False
    migrate_locations(ws, aws_profile="profile")
    ws.external_locations.list.assert_called()


def test_missing_aws_cli(ws, caplog, mocker):
    # Test to verify the CLI is called. Fail it intentionally to test the error message.
    mocker.patch("shutil.which", return_value=None)
    ws.config.is_azure = False
    ws.config.is_aws = True
    ws.config.is_gcp = False
    migrate_locations(ws, aws_profile="profile")
    assert "Couldn't find AWS CLI in path. Please install the CLI from https://aws.amazon.com/cli/" in caplog.messages


def test_migrate_locations_gcp(ws, caplog):
    ws.config.is_azure = False
    ws.config.is_aws = False
    ws.config.is_gcp = True
    migrate_locations(ws)
    assert "migrate_locations is not yet supported in GCP" in caplog.messages


def test_create_catalogs_schemas(ws):
    prompts = MockPrompts({'.*': 's3://test'})
    create_catalogs_schemas(ws, prompts)
    ws.catalogs.list.assert_called_once()


def test_cluster_remap(ws, caplog):
    prompts = MockPrompts({"Please provide the cluster id's as comma separated value from the above list.*": "1"})
    ws = create_autospec(WorkspaceClient)
    ws.clusters.get.return_value = ClusterDetails(cluster_id="123", cluster_name="test_cluster")
    ws.clusters.list.return_value = [
        ClusterDetails(cluster_id="123", cluster_name="test_cluster", cluster_source=ClusterSource.UI),
        ClusterDetails(cluster_id="1234", cluster_name="test_cluster1", cluster_source=ClusterSource.JOB),
    ]
    installation = create_autospec(Installation)
    installation.save.return_value = "a/b/c"
    cluster_remap(ws, prompts)
    assert "Remapping the Clusters to UC" in caplog.messages


def test_cluster_remap_error(ws, caplog):
    prompts = MockPrompts({"Please provide the cluster id's as comma separated value from the above list.*": "1"})
    ws = create_autospec(WorkspaceClient)
    ws.clusters.list.return_value = []
    installation = create_autospec(Installation)
    installation.save.return_value = "a/b/c"
    cluster_remap(ws, prompts)
    assert "No cluster information present in the workspace" in caplog.messages


def test_revert_cluster_remap(ws, caplog, mocker):
    prompts = MockPrompts({"Please provide the cluster id's as comma separated value from the above list.*": "1"})
    ws = create_autospec(WorkspaceClient)
    ws.workspace.list.return_value = [ObjectInfo(path='/ucx/backup/clusters/123.json')]
    with pytest.raises(TypeError):
        revert_cluster_remap(ws, prompts)


def test_revert_cluster_remap_empty(ws, caplog):
    prompts = MockPrompts({"Please provide the cluster id's as comma separated value from the above list.*": "1"})
    ws = create_autospec(WorkspaceClient)
    revert_cluster_remap(ws, prompts)
    assert "There is no cluster files in the backup folder. Skipping the reverting process" in caplog.messages
