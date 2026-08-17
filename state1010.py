import asyncio, os
import asyncpg
async def main():
    dsn=os.environ["DATABASE_URL"].replace("+asyncpg","").replace("+psycopg2","").replace("+psycopg","")
    c=await asyncpg.connect(dsn)
    r=await c.fetch("select id,state,is_completed,gathered_context->>'call_disposition' as dispo, created_at from workflow_runs where id=1010")
    print(dict(r[0]) if r else "no row")
    await c.close()
asyncio.run(main())
