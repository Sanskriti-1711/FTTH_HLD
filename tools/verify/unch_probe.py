import math, os, sys
from osgeo import ogr
D = sys.argv[1]
def verts(g):
    if g is None or g.IsEmpty(): return []
    w = g.ExportToWkt(); b = w[w.index("(")+1:].replace("(","").replace(")","")
    out=[]
    for p in b.split(","):
        t=p.strip().split()
        if len(t)>=2:
            try: out.append((float(t[0]),float(t[1])))
            except ValueError: pass
    return out
def pseg(p,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]; L=dx*dx+dy*dy
    if L<=0: return math.hypot(p[0]-a[0],p[1]-a[1])
    t=max(0.0,min(1.0,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/L))
    return math.hypot(p[0]-(a[0]+t*dx), p[1]-(a[1]+t*dy))
def dpath(p,v): return min((pseg(p,v[i],v[i+1]) for i in range(len(v)-1)), default=1e18)
ds=ogr.Open(D+"/Chambers.gpkg"); ch=[]
for f in ds.GetLayer(0):
    g=f.GetGeometryRef()
    if g: 
        c=g.Centroid().GetPoint(0); ch.append((c[0],c[1]))
ds=None
print("chambers:",len(ch))
ds=ogr.Open(D+"/Distribution_Ducts.gpkg"); lyr=ds.GetLayer(0)
print("=== Unchambered rows ===")
tot=0
for f in lyr:
    if str(f.GetField("SPAN_KIND") or "")!="Unchambered": continue
    tot+=1
    v=verts(f.GetGeometryRef())
    if len(v)<2: continue
    ds5=[dpath(c,v) for c in ch]
    n5=sum(1 for d in ds5 if d<=5); n10=sum(1 for d in ds5 if d<=10); n15=sum(1 for d in ds5 if d<=15)
    e0,e1=min((dpath(c,[v[0],v[0]]) for c in ch)), min((dpath(c,[v[-1],v[-1]]) for c in ch))
    print("  %-24s len=%7.1f  verts=%2d  ch<=5m=%2d <=10m=%2d <=15m=%2d  endD=%.1f/%.1f  S=%s E=%s"
          % (f.GetField("DUCT_ID"), float(f.GetField("length_m") or 0), len(v), n5,n10,n15, e0,e1,
             bool(str(f.GetField("START_CHAMBER") or "").strip()), bool(str(f.GetField("END_CHAMBER") or "").strip())))
ds=None
print("total unchambered:", tot)
