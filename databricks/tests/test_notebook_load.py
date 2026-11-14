"""Exercise the packaged notebook's load policy with an in-memory Spark adapter.

These are behavioral regression tests for create/retry/conflict handling. They
do not certify Databricks SQL syntax, Spark Connect or Delta runtime support.
The actual persist-delta cell is executed; its implementation is not copied.
"""

from collections import Counter
from contextlib import redirect_stdout
import io
import re
import unittest

from build_notebook import build_notebook


class ExistingTargetError(RuntimeError):
    pass


class Frame:
    def __init__(self, spark, rows, schema=("gpu_id", "value")):
        self.spark = spark
        self.rows = tuple(tuple(row) for row in rows)
        self.schema = schema

    def exceptAll(self, other):
        remaining = Counter(self.rows) - Counter(other.rows)
        return Frame(self.spark, list(remaining.elements()), self.schema)

    def limit(self, amount):
        return Frame(self.spark, self.rows[:amount], self.schema)

    def count(self):
        return len(self.rows)

    def orderBy(self, *columns):
        return Frame(self.spark, sorted(self.rows), self.schema)

    def createOrReplaceTempView(self, name):
        self.spark.views[name] = self


class Command:
    def __init__(self, execute):
        self.execute = execute

    def collect(self):
        # Deferring creation also exercises completion-before-view-cleanup.
        self.execute()
        return []


class Catalog:
    def __init__(self, spark):
        self.spark = spark

    def tableExists(self, name):
        return name in self.spark.tables

    def dropTempView(self, name):
        existed = name in self.spark.views
        self.spark.views.pop(name, None)
        self.spark.dropped_views.append(name)
        return existed


class MemorySpark:
    """Only the small interface used by the notebook load cell is represented."""

    def __init__(self):
        self.tables = {}
        self.views = {}
        self.created_tables = []
        self.dropped_views = []
        self.catalog = Catalog(self)
        self.concurrent_target = None

    def table(self, name):
        return self.tables[name]

    def createDataFrame(self, rows, schema):
        return Frame(self, rows, schema)

    def sql(self, statement):
        match = re.fullmatch(
            r"CREATE TABLE (`[^`]+`\.`[^`]+`\.`[^`]+`) "
            r"USING DELTA AS SELECT \* FROM `([^`]+)`",
            statement,
        )
        if not match:
            raise AssertionError(f"Unexpected load command: {statement}")
        target, view = match.groups()

        def execute():
            if target == self.concurrent_target:
                self.tables[target] = Frame(self, [("other-writer", 999)])
            if target in self.tables:
                raise ExistingTargetError(target)
            source = self.views[view]
            self.tables[target] = Frame(self, source.rows, source.schema)
            self.created_tables.append(target)

        return Command(execute)


class NotebookLoadTests(unittest.TestCase):
    def setUp(self):
        notebook = build_notebook()
        cell = next(cell for cell in notebook["cells"] if cell["id"] == "persist-delta")
        self.code = compile("".join(cell["source"]), "notebook:persist-delta", "exec")
        self.spark = MemorySpark()
        self.names = ("inventory", "telemetry", "workloads", "cooling")
        self.frames = {
            name: Frame(self.spark, [(f"{name}-1", 1), (f"{name}-2", 2)])
            for name in self.names
        }
        self.qualified = {name: f"`workspace`.`gpu_ops_test`.`{name}`" for name in self.names}

    def run_cell(self):
        namespace = {
            "spark": self.spark,
            "CATALOG": "workspace",
            "SCHEMA": "gpu_ops_test",
            "SCHEMAS": dict.fromkeys(self.names),
            "frames": self.frames,
            "expected_counts": {name: frame.count() for name, frame in self.frames.items()},
            "display": lambda value: None,
        }
        with redirect_stdout(io.StringIO()):
            exec(self.code, namespace)

    def test_initial_load_creates_all_four_tables_and_cleans_views(self):
        self.run_cell()
        self.assertEqual(set(self.spark.tables), set(self.qualified.values()))
        for name, frame in self.frames.items():
            stored = self.spark.tables[self.qualified[name]]
            self.assertEqual(stored.rows, frame.rows)
            self.assertEqual(stored.schema, frame.schema)
        self.assertEqual(len(self.spark.created_tables), 4)
        self.assertEqual(len(self.spark.dropped_views), 4)
        self.assertEqual(self.spark.views, {})

    def test_identical_rerun_preserves_rows_without_new_writes(self):
        self.run_cell()
        original = {name: frame.rows for name, frame in self.spark.tables.items()}
        self.run_cell()
        self.assertEqual({name: frame.rows for name, frame in self.spark.tables.items()}, original)
        self.assertEqual(len(self.spark.created_tables), 4)
        self.assertEqual(len(self.spark.dropped_views), 4)

    def test_any_existing_conflict_stops_all_table_writes(self):
        target = self.qualified["cooling"]
        # Same number of rows but changed values: count-only validation is insufficient.
        existing = Frame(self.spark, [("cooling-1", 999), ("cooling-2", 2)])
        self.spark.tables[target] = existing
        with self.assertRaisesRegex(ValueError, "cooling: data differs"):
            self.run_cell()
        self.assertEqual(self.spark.tables, {target: existing})
        self.assertEqual(self.spark.created_tables, [])
        self.assertEqual(self.spark.views, {})

    def test_concurrent_target_collision_propagates_and_cleans_source_view(self):
        target = self.qualified["inventory"]
        self.spark.concurrent_target = target
        with self.assertRaises(ExistingTargetError):
            self.run_cell()
        self.assertEqual(self.spark.tables[target].rows, (("other-writer", 999),))
        self.assertEqual(self.spark.created_tables, [])
        self.assertEqual(self.spark.views, {})
        self.assertEqual(len(self.spark.dropped_views), 1)


if __name__ == "__main__":
    unittest.main()
