# İş arama profili

SAVAS AI ilan süzgecinin kuralları (J-3, LLM yok). Aşağıdaki `ayarlar` bloğunu düzenle; her satır `anahtar: değer, değer, …`.
Büyük/küçük harf fark etmez. Bir ilan **başlığı** `roller`dan birini içermeli, `dislanan`lardan hiçbirini içermemeli.
Remotive ilanlarında aday konumu `remote_bolgeler`den birini içermeli ve iş tam zamanlı olmalı.
LinkedIn / Kariyer.net uyarılarında yer ve tür kayıtlı aramandan gelir; burada yalnız başlık, şirket ve deneyim süzülür.

```ayarlar
roller: backend, back-end, back end, full stack, full-stack, fullstack, yazılım geliştirici, yazılım geliştirme uzmanı, yazılım mühendisi, software developer, software engineer, python developer, java developer, .net developer, c# developer, node.js developer, php developer, project manager, proje yöneticisi, proje yöneticiliği, it project manager, technical project manager, business analyst, iş analisti, it business analyst, iş analizi uzmanı, system analyst, sistem analisti
dislanan: intern, internship, stajyer, staj, principal, director, direktör, head of, vp of, chief, frontend, front-end, ios, android, mobile, sales, satış
kara_liste: 
remote_bolgeler: worldwide, anywhere, europe, emea, eu, cet, eet, turkey, türkiye, poland, czech, czechia, bulgaria, romania, eastern europe
en_fazla_deneyim_yili: 7
bildirim_esigi: 70
tercihler: 
```

`bildirim_esigi`: Oracle'ın 0–100 uygunluk puanı bu değer ve üstündeyse telefona bildirim gelir (yoksa 70). `tercihler`: puanlamaya serbest metin (ör. maaş, şehir).

Notlar: deneyim, açıklamada "8+ years experience", "en az 8 yıl deneyim" gibi ifadelerden okunur (yalnız açıklaması olan ilanlarda).
