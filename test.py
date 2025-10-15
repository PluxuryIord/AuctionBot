#!/usr/bin/python
import os
import psycopg2

conn = psycopg2.connect(
    host="815be3a8cf75f455757d40e5.twc1.net",
    database="default_db",
    user="gen_user",
    password=r"DZXDt3{$u;W8gc",
    sslmode='verify-full',
    sslrootcert=os.path.expanduser('~\\.cloud-certs\\root.crt')
)

print(conn)