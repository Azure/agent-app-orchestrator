import pytest

from plugins.nl2sql.plugin import NL2SQLPlugin
from plugins.nl2sql.sql_validation import validate_single_read_only_select


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1; UPDATE dbo.Orders SET Amount = 0; COMMIT",
        "SELECT 1; DELETE FROM dbo.Orders",
        "SELECT 1; DROP TABLE dbo.Orders",
        "SELECT 1; EXEC dbo.RefreshOrders",
        "  SELECT 1;\n/* hidden write */\nUPDATE dbo.Orders SET Amount = 0",
        "SELECT 1;\n-- hidden write\nDELETE FROM dbo.Orders",
        "SELECT 1; ; DROP TABLE dbo.Orders",
        "SELECT * INTO dbo.OrderSnapshot FROM dbo.Orders",
        "SELECT * FROM OPENQUERY([linked], 'UPDATE dbo.Orders SET Amount = 0')",
        "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=server;', 'DELETE FROM dbo.Orders')",
        "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Data Source=server').db.dbo.Orders",
        "UPDATE dbo.Orders SET Amount = 0",
    ],
)
def test_validate_single_read_only_select_rejects_unsafe_queries(query):
    result = validate_single_read_only_select(query)

    assert result.is_valid is False
    assert result.error


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT 1;",
        """
        /* dashboard summary */
        SELECT TOP (10)
            o.OrderId,
            c.Name AS CustomerName,
            SUM(o.Amount) AS TotalAmount
        FROM dbo.Orders AS o
        INNER JOIN dbo.Customers AS c
            ON c.CustomerId = o.CustomerId
        WHERE o.CreatedAt >= '2026-01-01'
            AND o.Status IN ('Open', 'Closed')
        GROUP BY o.OrderId, c.Name
        HAVING SUM(o.Amount) > 100
        ORDER BY TotalAmount DESC;
        """,
        """
        SELECT
            p.ProductId,
            p.Name
        FROM dbo.Products p
        WHERE p.IsActive = 1
        ORDER BY p.Name
        OFFSET 0 ROWS FETCH NEXT 25 ROWS ONLY
        """,
        """
        WITH regional_orders AS (
            SELECT
                Region,
                COUNT(*) AS OrderCount
            FROM dbo.Orders
            GROUP BY Region
        )
        SELECT Region, OrderCount
        FROM regional_orders
        WHERE OrderCount > 10
        ORDER BY OrderCount DESC;
        """,
        """
        SELECT
            Region,
            COUNT(*) AS OrderCount
        FROM dbo.Orders
        GROUP BY Region
        ORDER BY OrderCount DESC
        LIMIT 10
        """,
    ],
)
def test_validate_single_read_only_select_allows_normal_select_queries(query):
    result = validate_single_read_only_select(query)

    assert result.is_valid is True
    assert result.error is None


@pytest.mark.asyncio
async def test_validate_sql_query_uses_read_only_validation():
    plugin = NL2SQLPlugin.__new__(NL2SQLPlugin)

    result = await plugin.validate_sql_query("SELECT 1; DROP TABLE dbo.Orders")

    assert result.is_valid is False
    assert result.error == "Only one SQL statement is allowed."


@pytest.mark.asyncio
async def test_execute_sql_query_rejects_unsafe_query_before_datasource_access():
    plugin = NL2SQLPlugin.__new__(NL2SQLPlugin)

    result = await plugin.execute_sql_query("sales", "SELECT 1; DELETE FROM dbo.Orders")

    assert result.results is None
    assert result.error == "Only one SQL statement is allowed."


def test_native_query_tool_never_strips_stacked_statements():
    # Native tools accept the complete model argument, without the retired
    # strategy's SQL text extractor between generation and validation.
    query = "SELECT 1;\n/* hidden write */\nDROP TABLE dbo.Orders;"
    assert not validate_single_read_only_select(query).is_valid
