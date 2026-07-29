import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv('DB_HOST'),
    database=os.getenv('DB_NAME'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    port=os.getenv('DB_PORT', 5432)
)

cur = conn.cursor()
cur.execute("""
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_name = 'ventas'
    ORDER BY ordinal_position
""")

columns = cur.fetchall()
print("Columnas de la tabla 'ventas':")
print("=" * 70)
for col_name, data_type, is_nullable in columns:
    nullable = "NULL" if is_nullable == 'YES' else "NOT NULL"
    print(f"{col_name:30} {data_type:15} {nullable}")

cur.close()
conn.close()
