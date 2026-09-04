import oracledb

c = oracledb.connect(user="SYSTEM", password="lxf82073077",
                     dsn="localhost:1521/FREEPDB1")
cur = c.cursor()

print("=== Oracle Database Free instance-level resource params ===")
names = ["cpu_count", "parallel_max_servers", "parallel_servers_target",
         "db_block_size", "memory_target", "memory_max_target",
         "sga_target", "sga_max_size", "pga_aggregate_target",
         "pga_aggregate_limit", "processes", "sessions",
         "resource_manager_plan", "parallel_degree_policy"]
ph = ",".join(":" + str(i) for i in range(len(names)))
cur.prepare("SELECT name, value, display_value, isdefault "
            "FROM v$parameter WHERE name IN (" + ph + ")")
cur.execute(None, names)
for r in cur.fetchall():
    print(f"{r[0]:26} value={str(r[1]):>16}  display={str(r[2]):>10}  isdefault={r[3]}")

print("\n=== version banner ===")
cur.execute("SELECT banner FROM v$version")
for r in cur.fetchall():
    print(r[0])

print("\n=== SGA/PGA current/max from v$sgainfo-ish (manual) ===")
for nm in ["sga_target", "pga_aggregate_limit"]:
    cur.execute("SELECT name,value FROM v$parameter WHERE name=:1", [nm])
    for r in cur.fetchall():
        print(r[0], r[1])

print("\n=== concurrency headroom: current sessions/processes usage ===")
cur.execute("SELECT COUNT(*) FROM v$session")
print("current sessions:", cur.fetchone()[0])
cur.execute("SELECT value FROM v$parameter WHERE name='processes'")
print("processes limit:", cur.fetchone()[0])
c.close()
