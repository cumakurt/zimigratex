# zimigrate

[English documentation](README.md)

`zimigrate`, bir Zimbra sunucusundaki hesapları, domainleri, listeleri, ayarları ve
mailbox içeriklerini kaldığı yerden devam edebilen yerel bir arşive aktarır.
Bu arşiv kullanıcı tarafından hedef Zimbra sunucusuna taşındıktan sonra yine yerel
olarak içe alınır. Export ve import tek bir Zimbra sürümüne kilitlenmez;
`zmprov`, `zmmailbox` ve `zmcontrol` komutlarının varlığını denetler. Uygulama SSH ile
başka bir sunucuya bağlanmaz ve export bittiğinde import işlemini kendiliğinden
başlatmaz.

## En basit kullanım

`zimigrate` paketini hem eski hem yeni Zimbra sunucusuna kurun. Depoyu `vendor/`
diziniyle birlikte kopyalayın. `export.sh` ve `import.sh` önce bu dizindeki sabit
CPython 3.12 çalışma zamanını, pip wheel'lerini ve CA paketini kullanır; OS Python
paketleri veya GitHub indirmesi yalnızca `vendor/` yoksa devreye girer. `export_data`
bulunduğunuz dizinde oluşur; yeterli boş alan olan bir volume'e geçip şunu çalıştırın:

```bash
/path/to/zimigratex/export.sh
```

Komut yerel Zimbra kurulumundaki aktarılabilir
verileri dışa aktarır ve bulunduğunuz dizinde şu klasörü oluşturur:

```text
./export_data/
```

Export sırasında `Ctrl+C` çalışan Zimbra komutlarını hemen durdurur. Aynı dizinde
`./export.sh` veya `zimigrate export` komutunu tekrar çalıştırın:

```bash
./export.sh
```

Başarılı tamamlanan hesaplar ve mailbox parçaları atlanır; yalnızca eksik veya hatalı
kalan işlemler devam eder. Devam işlemi kaynak hosta, Zimbra sürümüne, kapsama ve export
seçeneklerine bağlıdır. Başarılı bir checkpoint yalnızca kayıt dosyası ve bağlı bütün
mailbox parçaları kaydedilmiş checksum ve boyutla hâlâ eşleşiyorsa atlanır. Export
tamamlanınca import otomatik başlamaz.

## Bir domain veya tek hesap yedekleme

`export.sh` ve `import.sh` ekstra argümanları `zimigrate`'e iletir. `--user` veya
`--domain` (tekrarlanabilir; virgülle ayrılmış değerler de olur) işi bir mailbox veya
bir domain ve ona bağlı nesnelerle sınırlar. Kapsamlı çalışmada kategori sorusu
atlanır; global ve sunucu ayarları kopyalanmaz.

```bash
./export.sh --user user@example.com
./export.sh --domain example.com
./export.sh --archive ./backup_example --domain example.com
```

Import, **tam** bir arşive aynı süzgeci uygulayabilir; her şeyi bir kez export edip
sonra tek bir mailbox'ı geri yükleyebilirsiniz:

```bash
./import.sh --user user@example.com
./import.sh --domain example.com
```

`--user` o hesabı, domainini, COS'unu ve mailbox'ını geri yükler. `--domain` ayrıca
alias domainleri, hesapları ve dağıtım listelerini alır. Aynı kapsamlı komutla devam
edin; `--user`/`--domain` değiştirmek için export'ta yeni arşiv dizini, import'ta taze
`state.sqlite3` olan bir arşiv kopyası gerekir.

Export işlemi tamamen durduktan sonra `export_data` klasörünü olduğu gibi yeni sunucuya
manuel olarak taşıyın. Dosya izinleriyle birlikte `manifest.json` dosyasının da
korunması gerekir. Örneğin bağlı bir haricî disk veya
ağ diski kullanılıyorsa:

```bash
cp -a export_data /mnt/transfer/
```

Export başlamadan önce `zimigrate`, domain/COS, hesaplar, mailbox içerikleri, dağıtım
listeleri, global ayarlar ve sunucu ayarlarını ayrı ayrı gösterir. Enter varsayılanların
tamamını seçer; hesap veya mailbox seçimi gerekli bağımlılıkları otomatik ekler.
`zmprov gqu` kullanılarak mailbox boyutları, arşiv büyümesi ve worker başına geçici alan
hesaplanır. Yetersiz boş alan varsa işlem veri yazmadan durur. Rapor:
`export_data/reports/export-disk-assessment.json`.

Yeni Zimbra sunucusunda kopyalanan klasörü çalışacağınız dizine şu adla yerleştirin:

```text
./export_data/
```

Ardından yalnızca şu komutu çalıştırın:

```bash
/path/to/zimigratex/import.sh
```

Komut hedef Zimbra üzerinde herhangi bir değişiklik yapmadan önce özgün SQLite checkpoint
veritabanını, bütün aktarım kayıtlarının ve mailbox dosyalarının SHA-256 değerlerini,
manifest sayılarını ve ZIP/TGZ yapılarını doğrular; bağlantısız dosyaları reddeder.
Ardından ihtiyaç duyulan her `zmprov`/`zmmailbox` komutunun kurulu sürümde bulunduğunu
kontrol eder. Bu denetimlerin tamamı başarılı olursa yerel import işlemini başlatır.

Arşiv doğrulamasından sonra import da aynı kategori menüsünü gösterir. Global ve sunucu
ayarları yalnızca açık allowlist ile seçilebilir. Hedefteki `zmvolume -l` message/index
volume yolları ile geçici alan, herhangi bir hedef değişikliğinden önce kontrol edilir.
Yetersiz alan varsa import durur; rapor `export_data/reports/import-disk-assessment.json`
dosyasına yazılır. Yerel proses eşlenen uzak bir mailbox hostun diskini ölçemediği için
böyle bir eşleme varsayılan olarak importu durdurur. Her uzak message/index volume ayrı
olarak denetlendikten sonra sorumluluğu açıkça kabul etmek için
`allow_unverified_remote_capacity = true` kullanılabilir.

Import kesilirse aynı dizinde tekrar çalıştırın:

```bash
./import.sh
```

Tamamlanan aşamalar SQLite checkpoint üzerinden atlanır. Import bittikten sonra hedef
nesneler, taşınabilir öznitelikler, alias'lar, kimlikler, imzalar, veri kaynakları,
dağıtım listesi üyeleri ve mailbox checkpoint'leri otomatik olarak arşivle
karşılaştırılır. Bağımsız doğrulama kategori, eşleme ve opsiyonel yapılandırma
politikalarını checkpoint'ten okur. Daha sonra tekrar çalıştırmak için:

```bash
zimigrate verify-target
```

Tamamlanmamış hesaplar eşzamanlı kullanıcı yazımlarını önlemek için `maintenance`
durumunda tutulur; hesabın kaynak durumuna dönüşü ancak bütün metadata ve mailbox
aşamaları başarıyla bittikten sonra yapılır. Hesap oluşturma ve parola hash'leri
`zmprov -l` ile doğrudan LDAP'e yazılır, böylece Zimbra `{SSHA}` değerini düz metin
gibi yeniden hash'lemez. Bu yazımdan sonra zimigrate SOAP `zmprov fc account`
çalıştırır; mailboxd önbelleğindeki boş parola veya `maintenance` durumu
`ldap_cache_account_maxage` (varsayılan 15 dakika) dolana kadar kullanılmaz. Cache
yenilemesi başarısız olursa import durur ve hesap tekrar `maintenance` durumuna alınır.

## Import öncesi yapılan doğrulamalar

`zimigrate import`, aşağıdaki kontrollerin hepsini import başlamadan önce yapar:

- Manifestin tamamlanmış ve desteklenen şemada olması
- Özgün `state.sqlite3` checkpoint veritabanının mevcut ve sağlam olması
- Her domain, COS, hesap, kaynak ve dağıtım listesi kaydının okunabilmesi
- Her provisioning kaydının checkpoint SHA-256 değeriyle eşleşmesi
- Manifestteki nesne sayılarıyla bulunan nesne sayılarının eşleşmesi
- Her mailbox dosyasının kaydedilmiş boyut ve SHA-256 değeriyle eşleşmesi
- Manifest veya kayıtlarda referansı olmayan nesne/mailbox dosyalarının bulunmaması
- Bütün ZIP/TGZ dosyalarının açılabilir, bozulmamış ve güvenli dosya yollarına sahip
  olması

Bu kontrollerden biri başarısız olursa import nesnesi oluşturulmaz ve hedef Zimbra'da
değişiklik yapılmaz. Hata giderildikten veya klasör yeniden kopyalandıktan sonra
`zimigrate import` tekrar çalıştırılabilir.

## Export edilen veriler

- Domainler, alias domainler ve domain öznitelikleri
- Class of Service (COS) tanımları ve kaynak-hedef kimlik eşlemeleri
- Kullanıcı hesapları ve takvim kaynakları
- Parola hash'leri, alias'lar, tercihler, filtreler ve yönlendirmeler
- Kimlikler, imzalar ve desteklenen haricî veri kaynakları
- Statik ve dinamik dağıtım listeleri, alias'ları, öznitelikleri ve statik üyeleri
- Postalar, takvimler, kişiler, görevler ve Briefcase içerikleri
- Global ve sunucu bazlı LDAP yapılandırma görüntüleri
- `zimbraACE` içindeki taşınabilir kaynak UUID'lerinin hedef UUID'lerine eşlenmesi

Canlı kimlik doğrulama token'ları hiçbir zaman export edilmez. Zimbra sistem hesaplarının
metadata bilgileri arşivlenir; ancak sistem mailbox içerikleri ve hedef kurulumun servis
kimlikleri varsayılan olarak aktarılmaz. Bunları körlemesine değiştirmek hedef Zimbra
kurulumunu bozabilir.

Global ve sunucu ayarları arşivlenir fakat varsayılan import sırasında uygulanmaz.
Hostname, sertifika, port, LDAP/MTA topolojisi ve sunucu kimliği içeren bu ayarlar yalnızca
incelenmiş bir öznitelik allowlist'iyle etkinleştirilebilir. Normal kullanıcı hesapları,
domainler, COS'lar, dağıtım listeleri ve mailbox içerikleri varsayılan akışta aktarılır.

Zimbra dört veri kaynağı credential alanını kaynak `zimbraDataSourceId` değerine bağlı
biçimde kodlar. Export bu alanları yalnızca proses içinde Zimbra'nın uzun süredir
kullandığı LDAP kodlamasıyla çözer, plaintext'i arşive yazar ve hedefin
yeni veri kaynağı ID'si ile yeniden şifrelemesini sağlar. Import sırasında veri kaynağı,
bütün öznitelik ve credential değerleri uygulanana kadar kapalı tutulur. LDAP ciphertext'ini
doğrudan kopyalamak kullanılamayan credential üretirdi.

## Gereksinimler

- Python 3.11 veya üzeri
- Python `rich` paketi
- Kurulu Zimbra sürümünün desteklediği 64-bit x86_64 glibc Linux
- `zimigrate` paketinin hem kaynak hem hedef sunucuda kurulu olması
- Yerel sunucuda `/opt/zimbra/bin/zmprov`, `zmmailbox`, `zmcontrol` ve `zmhostname`
- `zimbra` kullanıcısı olarak çalışma veya yerel `sudo -n -u zimbra` yetkisi
- Arşiv için yeterli alan ve worker başına bir şifresiz mailbox parçası kadar geçici alan
- Preflight'tan geçen yerel bir Zimbra FOSS kurulumu

Kurulumu depo kökünden yapın (`pyproject.toml` dosyasının bulunduğu dizin;
`src/zimigrate` değil). Desteklenen işletici yolu sarmalayıcı betiklerdir:

```bash
/path/to/zimigratex/export.sh
/path/to/zimigratex/import.sh
```

Python 3.11+, sanal ortam ve repository kaynaklarıyla güncel bir `zimigrate` kurulumu
zaten hazırsa betikler OS paket kurulumu ve env oluşturmayı atlar. Kaynak kod değişirse
runtime damgası geçersizleşir ve eski `.venv` içindeki kodun sessizce çalışması yerine
paket yeniden kurulur. Ek argümanlar iletilir; örneğin
`./export.sh --archive /srv/migration/export_data`.

Depoyu `vendor/` ile birlikte kopyalayın. Sarmalayıcılar `vendor/python/` içindeki
x86_64 glibc CPython arşivini açar, `rich` paketini `vendor/wheels/`
üzerinden PyPI'ye bağlanmadan kurar ve hâlâ indirme gerekirse
`vendor/certs/cacert.pem` kullanır. Bu dosyaları interneti olan bir makinede yenilemek
için:

```bash
./scripts/vendor-runtime.sh
```

`vendor/` yoksa ve OS Python 3.11+ kurulamazsa sarmalayıcılar aynı sabitlenmiş
bağımsız CPython 3.12 çalışma zamanını indirir, SHA-256 özetini doğrular ve devam eder.
`python3-venv` gibi isteğe bağlı paketlerin yokluğu `ca-certificates` kurulumunu
engellemez. TLS güven deposu GitHub'ı doğrulayamazsa indirme CA paketinden sonra ve
son çare olarak TLS doğrulaması olmadan yinelenir; sabit SHA-256 özeti değiştirilmiş
bir arşivi yine reddeder. Sanal ortam hâlâ oluşturulamazsa `rich`
`.runtime/` altına kurulur ve `zimigrate` depo `src/` ağacından çalışır.

Elle kurulum da kullanılabilir:

```bash
cd /path/to/zimigratex
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Export ve import etkileşimli terminalde host, envanter, disk durumu ve aşama ilerlemesini
aynı ekranda güncelleyen canlı bir panel gösterir. `--verbose`, `--json-logs`, TTY olmayan
çıktı, `TERM=dumb` veya `ZIMIGRATE_PLAIN_OUTPUT=1` klasik satır tabanlı logları kullanır.

Varsayılan kullanım için yapılandırma dosyası gerekmez. Export kaynak makinedeki, import
ise hedef makinedeki yerel Zimbra komutlarını çalıştırır. Uygulama hiçbir hedefe SSH
komutu göndermez.

Çok mailbox sunuculu bir kaynakta içerik, Zimbra'nın kendi yönetim REST arayüzü üzerinden
hesabın `zimbraMailHost` sunucusundan alınabilir. Bu davranış SSH ile komut çalıştırmak
değildir ve mailbox içeriğinin doğru düğümden alınması için gereklidir.

## Durum ve bağımsız doğrulama

Bütün komutlar varsayılan olarak geçerli dizindeki `export_data` klasörünü kullanır:

```bash
zimigrate status
zimigrate verify --deep
zimigrate verify-target
```

`verify --deep`, import komutunun her çalışmada otomatik
yaptığı arşiv doğrulamasını import başlatmadan ayrıca çalıştırır.

## İsteğe bağlı gelişmiş yapılandırma

Argümansız kullanım güvenli varsayımlarla gelir:

- Bütün normal hesaplar seçilir.
- Mailbox içerikleri ve görüntülenebilen parola hash'leri export edilir.
- Sekiz adet sınırlı worker kullanılır.
- Geçici hatalar sınırlı ve üstel gecikmeli olarak yeniden denenir.
- Hedefte var olan nesneler `merge` politikasıyla birleştirilir.
- Mailbox çakışmalarında güvenli `skip` politikası kullanılır.

Worker, timeout, hesap filtresi veya import politikası değiştirilecekse
[config.example.toml](config.example.toml) kullanılabilir:

```bash
cp config.example.toml migration.toml
zimigrate export --config migration.toml
zimigrate import --config migration.toml
```

Yapılandırma dosyası uzak komut yürütmeyi etkinleştiremez; iki komut da her zaman
yereldir. Gelişmiş seçenekler şunları kapsar:

- Worker sayısı, retry ve timeout değerleri
- Hesap include/exclude desenleri
- Tam veya yıl bazlı parçalı mailbox export'u
- ZIP veya eski sistemler için TGZ biçimi
- Çok mailbox sunuculu hedefte mailhost eşlemesi
- Mevcut nesne ve mailbox çakışma politikaları
- Global/sunucu öznitelikleri için incelenmiş allowlist'ler

Attribute uygulaması varsayılan olarak strict'tir. Hedef şemanın reddettiği bir
öznitelik, eksik tercih veya filtreyle sahte başarı üretmek yerine importu durdurur.
`strict_attributes = false` yalnızca uyarı raporu incelenerek kullanılmalıdır; servis,
bağlantı ve timeout hataları hiçbir zaman attribute uyarısına indirgenmez. Yıl parçaları
locale'den bağımsız UTC epoch sınırları kullanır ve çakışmaz. Conflict policy `reset`
ise yalnızca ilk parça mailbox'ı sıfırlar; sonraki parçalar önceki veriyi silmemek ve
resume'u güvenli tutmak için `skip` kullanır.

Mailbox export, Zimbra REST'e `meta=1` ve varsayılan olarak `lock=1` gönderir. Kurulu
Zimbra sürümü istenen lock seçeneğini reddederse export kilitsiz biçimde sessizce devam
etmez; durur. `mailbox_lock = false` yalnızca denetimli bir bakım penceresinde kullanılmalıdır.

Varsayılan `export_data` yolu da gerekirse değiştirilebilir:

```bash
zimigrate export --archive /srv/migration/export_data
zimigrate import --archive /srv/migration/export_data
```

Gelişmiş bir config import davranışını değiştiriyorsa bu dosyayı da hedefe manuel olarak
kopyalayıp import komutunda tekrar belirtin. İlk import denemesinin politikaları
checkpoint'e bağlanır ve devam eden bir import sırasında sessizce değiştirilemez.

## Güvenlik ve güvenilirlik

- Kayıtlar ve mailbox içerikleri düz metin olarak saklanır.
- Provisioning kayıtları ve mailbox dosyaları için SHA-256 bütünlük değerleri tutulur.
- Arşiv dizini `0700`, hassas dosyalar `0600` izinleriyle oluşturulur.
- Dosyalar atomik olarak yazılır.
- Özgün SQLite checkpoint veritabanı zorunludur; her bağımsız işlem buraya kaydedilir.
- Worker havuzları sınırlıdır; kontrolsüz thread oluşturulmaz.
- Hassas `zmprov` değerleri proses argümanları yerine stdin batch akışından verilir.
- Geçici mailbox parçaları yalnızca işlem sırasında `export_data/.tmp` altında tutulur
  ve sonra kaldırılır.

Mailbox protokolü Zimbra'nın
[resmî REST export/import referansını](https://github.com/Zimbra/zm-mailbox/blob/develop/store/docs/rest.txt)
izler. `zmprov`, `zmmailbox` ve cache komutları için operasyonel dayanak
[resmî komut satırı kılavuzudur](https://github.com/Zimbra/adminguide/blob/develop/cmdlineutils.adoc);
uygulama ihtiyaç duyduğu komutları hedefte değişiklik yapmadan önce doğrular.

`zimigrate` çalışırken `export_data` klasörünü kopyalamayın. Klasörü güvenilir, tercihen
disk şifrelemeli bir dosya sisteminde tutun. Arşiv hesap adları, secret export açıksa
parola hash'leri, mailbox içeriği ve checkpoint metadata bilgilerini içerir. Ayrıntılar
için [SECURITY.md](SECURITY.md) dosyasına bakın.

## Bilinen sınırlar

- Bu uygulama seviyesinde bir geçiştir; LDAP, MariaDB ve blob store'un birebir fiziksel
  geri yüklemesi değildir.
- Sertifikalar, özel anahtar dosyaları, `zmlocalconfig`, işletim sistemi paketleri, MTA
  kuyruğu, DNS, firewall ve ticari Network Edition backup verileri kurulmaz.
  Hesap `jpegPhoto`, `userCertificate` ve `userSMIMECertificate` LDAP ikili
  öznitelikleridir; `zmprov` argv ile DER/JPEG geri yazamaz, bu yüzden import atlar.
- Varsayılan imza kimlikleri hedef imzalar oluşturulduktan sonra yeniden eşlenir.
  `zimbraPrefMailSignatureContactId` bir kişi UUID'sidir ve hedefte boş bırakılır.
- Cross-mailbox paylaşım kimlikleri değişebilir. Paylaşımlar ve delege edilmiş klasör
  yetkileri geçiş sonrasında kontrol edilmeli, gerekirse yeniden oluşturulmalıdır.
- Hedef doğrulaması provisioning durumunu ve başarılı mailbox REST import checkpoint'lerini
  karşılaştırır; hedef mailbox içeriğini öğe öğe karşılaştırmaz.
- Kaynak sistem export sırasında değişmeye devam ederse cluster genelinde işlemsel bir
  snapshot oluşmaz. Son geçiş bakım veya yazma dondurma penceresinde yapılmalıdır.
- Import disk hesabı yalnızca sıkıştırılmış ZIP/TGZ boyutunu değil, arşiv üyelerinin
  açılmış boyutunu kullanır. Çok mailbox sunuculu hedefte uzak düğüm yolları yerel dosya
  sisteminden ölçülemez. Import, işletici her eşlenen hostu ayrıca denetleyip bu sınırı
  açıkça kabul etmedikçe uzak mailhost eşlemelerini reddeder.
- Yıl parçalı mailbox export, locale `mm/dd/yyyy` yerine Zimbra `DateQuery` sayısal UTC
  epoch milisaniyelerini (`date:<` / `date:>=`) kullanır. REST sorgu export'u Zimbra
  aramanın yaptığı gibi boş klasörleri ve aranabilir tarihi olmayan öğeleri atlayabilir;
  en eksiksiz kopya için `mailbox_mode = "full"` kullanın.
- Hedef sürüm kilidi isteğe bağlıdır. Varsayılan olarak `zmprov`/`zmmailbox`/`zmcontrol`
  çalıştırabilen her yerel Zimbra sürümü kabul edilir. Belirli bir `zmcontrol -v`
  çıktısını zorunlu kılmak için `import.expected_target_version_pattern` ayarlanır.

## Geliştirme ve test

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
ruff check src tests
ruff format --check src tests
bandit -q -r src
python -m compileall -q src tests
```

## Geliştirici

Cuma KURT

- GitHub: [https://github.com/cumakurt/zimigratex](https://github.com/cumakurt/zimigratex)
- LinkedIn: [https://www.linkedin.com/in/cuma-kurt-34414917/](https://www.linkedin.com/in/cuma-kurt-34414917/)

## Lisans

Copyright (C) 2026 Cuma KURT.

Bu program özgür yazılımdır: Özgür Yazılım Vakfı tarafından yayımlanan GNU
Affero Genel Kamu Lisansı'nın yalnızca 3. sürümü altında yeniden dağıtabilir
ve değiştirebilirsiniz. Ayrıntılar için [LICENSE](LICENSE) dosyasına bakın.
