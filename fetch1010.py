import asyncio, httpx
from api.services.storage import storage_fs
async def main():
    try:
        u=await storage_fs.aget_signed_url("transcripts/1010.txt",expiration=600,use_internal_endpoint=True)
        async with httpx.AsyncClient(timeout=30) as c:
            r=await c.get(u)
        if r.status_code==200:
            print("---- TRANSCRIPT run 1010 ----")
            print(r.text)
        else:
            print(f"(status {r.status_code} - transcript not ready yet)")
    except Exception as e:
        print("not ready:", repr(e)[:150])
asyncio.run(main())
