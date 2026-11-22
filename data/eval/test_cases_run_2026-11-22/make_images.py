import sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter
out=sys.argv[1]; reg=sys.argv[2]; bold=sys.argv[3]
F=lambda s,b=False: ImageFont.truetype(bold if b else reg, s)
def wrap(d,text,font,w):
    lines=[];cur=""
    for word in text.split():
        t=(cur+" "+word).strip()
        if d.textlength(t,font=font)>w: lines.append(cur);cur=word
        else: cur=t
    lines.append(cur);return lines
# F1: fake social post
im=Image.new("RGB",(900,420),"white");d=ImageDraw.Draw(im)
d.ellipse((30,30,100,100),fill="#4a6fa5")
d.text((120,35),"Wiadomości z kraju",font=F(28,True),fill="black")
d.text((120,72),"@wiadomosci_teraz · 19.11.2026",font=F(22),fill="#666")
y=130
for l in wrap(d,"PILNE! MSZ zakazuje wyjazdów do Włoch od 20 listopada! Strajk generalny paraliżuje kraj",F(34,True),830):
    d.text((35,y),l,font=F(34,True),fill="black");y+=48
d.text((35,y+30),"12:47 · 19 lis 2026 · 3,2 tys. wyświetleń",font=F(22),fill="#666")
im.save(f"{out}/F1_msz_zakaz.png")
# F2: GIS warning
im=Image.new("RGB",(1000,330),"white");d=ImageDraw.Draw(im)
d.rectangle((0,0,1000,70),fill="#1f4e79");d.text((30,18),"Główny Inspektorat Sanitarny – Ostrzeżenia",font=F(28,True),fill="white")
y=110
for l in wrap(d,"18.11.2026 Ostrzeżenie publiczne dotyczące żywności: stwierdzenie przekroczenia najwyższego dopuszczalnego poziomu ochratoksyny A w mielonej kawie",F(30),930):
    d.text((35,y),l,font=F(30),fill="black");y+=44
im.save(f"{out}/F2_gis_kawa.png")
# F3: unreadable
im=Image.open(f"{out}/F1_msz_zakaz.png").resize((90,42)).resize((900,420)).filter(ImageFilter.GaussianBlur(9))
im.save(f"{out}/F3_nieczytelne.png")
