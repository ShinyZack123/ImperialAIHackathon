# Paste this into the existing failed persistence cell, replacing that entire cell.
# Earlier notebook cells must already have run in this session.
qualified_names = {name: f"`{CATALOG}`.`{SCHEMA}`.`{name}`" for name in SCHEMAS}
already_loaded = set()
conflicts = []
for table_name, incoming in frames.items():
    qualified_name = qualified_names[table_name]
    if spark.catalog.tableExists(qualified_name):
        existing = spark.table(qualified_name)
        if existing.schema != incoming.schema:
            conflicts.append(f"{table_name}: schema differs")
            continue
        # exceptAll preserves duplicate counts. Check both directions.
        different = existing.exceptAll(incoming).limit(1).count() > 0 or incoming.exceptAll(existing).limit(1).count() > 0
        if different:
            conflicts.append(f"{table_name}: data differs")
        else:
            already_loaded.add(table_name)

if conflicts:
    raise ValueError("Existing tables were not changed. Choose a NEW SCHEMA name in the configuration cell and run all again. Conflicts: " + "; ".join(conflicts))

from uuid import uuid4

for table_name, incoming in frames.items():
    if table_name in already_loaded:
        print(f"Unchanged: {qualified_names[table_name]}")
    else:
        temp_view = f"_gpu_ops_load_{uuid4().hex}"
        incoming.createOrReplaceTempView(temp_view)
        try:
            # CREATE fails if another writer creates the target after preflight.
            # Collect finishes the command before its source view is removed.
            spark.sql(
                f"CREATE TABLE {qualified_names[table_name]} "
                f"USING DELTA AS SELECT * FROM `{temp_view}`"
            ).collect()
        finally:
            spark.catalog.dropTempView(temp_view)
        print(f"Created: {qualified_names[table_name]}")

counts = []
for table_name, expected in expected_counts.items():
    actual = spark.table(qualified_names[table_name]).count()
    if actual != expected:
        raise ValueError(f"Stored row count mismatch in {table_name}: {actual}, expected {expected}")
    counts.append((table_name, actual))
display(spark.createDataFrame(counts, "table_name STRING, row_count LONG"))
display(spark.table(qualified_names["inventory"]).orderBy("gpu_id"))
