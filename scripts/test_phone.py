import sys, pathlib, asyncio, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import httpx
from PIL import Image
import app.api as api

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"


async def main():
    results = {}
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # t1 — GET / serves the page (unauthenticated), contains "Nervice"
        r = await c.get("/")
        body = r.text
        idx = await c.get("/static/index.html")
        man = await c.get("/static/manifest.json")
        results["t1"] = r.status_code == 200 and "Nervice" in body and idx.status_code == 200 and man.status_code == 200
        print(f"t1: GET / = {r.status_code} contains-Nervice={'Nervice' in body} | "
              f"/static/index.html={idx.status_code} /static/manifest.json={man.status_code} "
              f"-> {'PASS' if results['t1'] else 'FAIL'}")

        # t2 — static mount does NOT open the API: /chat and /health still require auth
        chat_no = await c.post("/chat", json={"message": "hi"})
        health_no = await c.get("/health")
        results["t2"] = chat_no.status_code == 401 and health_no.status_code == 401
        print(f"t2: POST /chat no-token={chat_no.status_code} | GET /health no-token={health_no.status_code} "
              f"-> {'PASS' if results['t2'] else 'FAIL'}")

        # t3 — manifest valid + icons exist and are valid pngs
        manifest = json.loads((STATIC / "manifest.json").read_text())
        icon_srcs = {i["src"].split("/")[-1] for i in manifest.get("icons", [])}
        ok = manifest.get("name") == "Nervice" and {"icon-192.png", "icon-512.png"}.issubset(icon_srcs)
        for fn, exp in (("icon-192.png", (192, 192)), ("icon-512.png", (512, 512)), ("apple-touch-icon.png", (180, 180))):
            im = Image.open(STATIC / fn)
            served = await c.get(f"/static/{fn}")
            png_ok = im.format == "PNG" and im.size == exp and served.status_code == 200
            print(f"  {fn}: {im.format} {im.size} served={served.status_code} -> {'ok' if png_ok else 'FAIL'}")
            ok = ok and png_ok
        results["t3"] = ok
        print(f"t3: manifest+icons valid -> {'PASS' if results['t3'] else 'FAIL'}")

    print("\nRESULT:", "  ".join(f"{k}={'PASS' if v else 'FAIL'}" for k, v in results.items()))


asyncio.run(main())
