# kisisel-enflasyonum-veri

Kişisel Enflasyonum uygulaması için TÜİK tüketici fiyat endeksi (TÜFE) verisi.

## Ne işe yarar

Uygulama TÜİK verisini buradan alır. Telefonlar TÜİK'e değil, GitHub Pages
üzerinde yayınlanan küçük bir JSON dosyasına bağlanır. TÜİK'e yalnızca bu
reponun GitHub Actions iş akışı, ayda birkaç kez ve en az istekle bağlanır.

## Veri adresi

<https://f1r4vun.github.io/kisisel-enflasyonum-veri/v1/tufe.json>

Yol bilerek `v1/` içindedir. İleride biçim değişirse eski uygulama sürümleri
`v1`'i okumaya devam eder.

## Kaynak ve atıf

- Veri TÜİK'e (Türkiye İstatistik Kurumu) aittir.
- Veri, TÜİK veri tarayıcısının (`databrowser2.tuik.gov.tr`) belgelenmemiş arka
  ucundan alınır (akış `TR,DF_TUFE_SDMX_TT09,1.0`, harcama gruplarına göre endeks
  sonuçları, 2025=100). Bu servis haber verilmeden değişebilir.
- Kullanım koşulları için TÜİK'e bakılmalıdır.

## Dosya biçimi

| Alan | Açıklama |
| --- | --- |
| `schema` | Biçim sürümü, şu an `1`. |
| `source` | Kaynağın Türkçe tanımı. |
| `sourceUrl` | TÜİK veri portalı adresi. |
| `base` | Endeks bazı, `2025=100`. |
| `firstMonth` | Serilerin ilk ayı (`YYYY-MM`), varsayılan `2024-01`. |
| `latestMonth` | `"01"` (gıda ve alkolsüz içecekler) serisinin değeri olan en son ay. |
| `generatedAt` | Dosyanın üretildiği an (UTC, ISO 8601). |
| `names` | Kod → Türkçe ad. Önce `"0"` (genel TÜFE), sonra COICOP 2018 kodları. |
| `series` | Kod → değer listesi. `firstMonth`'tan `latestMonth`'a her ay için bir eleman; eksik ay `null`. |

Değerler 6 ondalığa yuvarlanır. Sonradan başlayan seriler (örneğin `01143`,
Aralık 2025) başta `null` içerir. Dosya yalnızca içerik değiştiğinde yeniden
yazılır; `generatedAt` tek başına değişmez.

## Güncelleme takvimi

`tufe` iş akışı her ayın 3–10. günleri saat 10:23'te (TR) çalışır. TÜİK veriyi
genelde ayın 3'ünde saat 10:00'da yayınlar. Dosyada bir önceki ayın verisi
zaten varsa TÜİK'e istek yapılmaz. Veri değişmişse dosya `main`'e commit edilir
ve GitHub Pages'e yayınlanır.

Ayın 10'u veya sonrasında bir önceki ayın verisi hâlâ yoksa iş akışı
"TÜİK verisi gecikti" hatasıyla başarısız olur.

## Doğrulama kuralları

Aşağıdakilerden biri olursa dosya yazılmaz ve iş akışı başarısız olur:

1. Genel TÜFE (`"0"`) ve `"01"` serilerinde son ay dahil hiç eksik ay olmamalı.
2. `scripts/names_tr.json` dosyasındaki her kodun en az bir değeri olmalı.
3. Her değer sonlu ve sıfırdan büyük olmalı.
4. Ardışık iki dolu ay arasındaki oran `"0"`, `"01"`, `"011"`, `"012"` için
   `[0,80; 1,30]`, diğerleri için `[0,40; 2,50]` aralığında olmalı.
5. 2025'in 12 ayı da dolu olan her kodun 2025 ortalaması `100 ± 0,5` olmalı
   (baz değişikliğini yakalar).
6. Son ay bir önceki takvim ayından ileride, eski dosyadaki son aydan geride
   olamaz.
7. Eski dosyadaki değerler binde 1'den fazla değişmemiş olmalı (revizyon).

Uyarı verilir ama dosya yine yazılır: `"0"` ve `"01"` dışındaki bir kodun son ay
değeri yoksa ya da serisinde boşluk varsa (boşluk `null` yazılır).

Betiğin çıkış kodları: `0` tamam, `1` doğrulama hatası, `2` ağ hatası,
`3` gecikme.

## Elle tetikleme

Actions → **tufe** → **Run workflow**. Güncel olsa da yeniden çekip yayınlamak
için `force` kutusunu işaretleyin.

Yerelde, TÜİK'e bağlanmadan denemek için:

```sh
python -m unittest discover -s tests -v
python scripts/tuik_fetch.py --out /tmp/tufe.json --input tests/fixtures/tt09_data.json \
  --names tests/fixtures/names_test.json --first-month 2025-01 --dry-run
```

## Hata olursa

İş akışı başarısız olursa "Otomatik TÜFE güncellemesi başarısız" başlıklı bir
issue açılır (açık olan varsa ona yorum eklenir). Yayınlanan dosya değişmez;
uygulama son sağlam veriyle çalışmaya devam eder.

## GitHub Pages kurulumu (tek seferlik)

Settings → Pages → Build and deployment → Source: **GitHub Actions**.

## Kişisel veri

Bu repo kişisel veri içermez; yalnızca TÜİK'in kamuya açık rakamları vardır.
