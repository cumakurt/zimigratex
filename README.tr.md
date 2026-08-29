# zimigrate

[English documentation](README.md)

`zimigrate`, bir Zimbra sunucusundaki hesapları, domainleri, listeleri ve mailbox
içeriklerini kaldığı yerden devam edebilen bir arşive aktarır. Bu arşiv hedef Zimbra
sunucusuna taşındıktan sonra yerel olarak içe alınır. Export ve import tek bir Zimbra
sürümüne kilitlenmez; `zmprov`, `zmmailbox` ve `zmcontrol` komutlarını yerinde
dener. Export bittiğinde import kendiliğinden başlamaz.

İki export yerleşimi vardır:

- **Yerel:** komutu Zimbra sunucusunda çalıştırın; arşiv bulunduğunuz dizine yazılır.
- **Uzak:** iş istasyonundan `./export.sh --target-ip HOST`; export o Zimbra’da SSH
  ile çalışır, arşiv her durumda sizin makinenizde kalır.

## İçindekiler

- [Kurulum ve çalıştırma](#kurulum-ve-çalıştırma)
- [Komutlar](#komutlar)
- [Kullanım alternatifleri](#kullanım-alternatifleri)
- [Etkileşimli menüler](#etkileşimli-menüler)
- [Export edilen veriler](#export-edilen-veriler)
- [Import öncesi doğrulama](#import-öncesi-doğrulama)
- [Gereksinimler](#gereksinimler)
- [İsteğe bağlı yapılandırma](#isteğe-bağlı-yapılandırma)
- [Güvenlik ve güvenilirlik](#güvenlik-ve-güvenilirlik)
- [Bilinen sınırlar](#bilinen-sınırlar)
- [Geliştirme ve test](#geliştirme-ve-test)

## Kurulum ve çalıştırma

Depoyu `vendor/` diziniyle birlikte kopyalayın. Yeterli boş alan olan bir volume’e
geçin: `export_data` **betiklerin yanında değil, bulunduğunuz çalışma dizininde**
oluşur.

Desteklenen yol `export.sh` ve `import.sh` sarmalayıcılarıdır. Önce `vendor/`
içindeki sabit x86_64 glibc CPython 3.12 çalışma zamanını, pip wheel’lerini ve CA
paketini kullanırlar. OS Python paketleri veya GitHub indirmesi yalnızca `vendor/`
yoksa devreye girer.

```bash
/path/to/zimigratex/export.sh
/path/to/zimigratex/import.sh
```

Python 3.11+, sanal ortam ve bu deponun güncel kurulumu zaten varsa betikler OS
paket kurulumunu atlar. Kaynak kod değişince runtime damgası geçersizleşir; eski
`.venv` içindeki kod sessizce çalışmaz, paket yeniden kurulur. Ek argümanlar
iletilir.

Vendored dosyaları interneti olan bir makinede yenilemek için:

```bash
./scripts/vendor-runtime.sh
```

`vendor/` yoksa ve OS Python 3.11+ kurulamazsa sarmalayıcılar aynı sabit CPython
3.12 arşivini indirir, SHA-256 özetini doğrular ve devam eder. `python3-venv` gibi
isteğe bağlı paketlerin yokluğu `ca-certificates` kurulumunu engellemez. TLS güven
deposu GitHub’ı doğrulayamazsa indirme CA paketinden sonra ve son çare olarak TLS
doğrulaması olmadan yinelenir; sabit özet değiştirilmiş bir arşivi yine reddeder.
Sanal ortam hâlâ oluşturulamazsa `rich` `.runtime/` altına kurulur ve `zimigrate`
depo `src/` ağacından çalışır.

Elle kurulum:

```bash
cd /path/to/zimigratex
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Bundan sonra `status`, `verify`, `verify-target` ve `preflight` için `zimigrate`
komutu `PATH` üzerindedir. Export ve import için yine sarmalayıcılar tercih edilir.

Varsayılan akış için yapılandırma dosyası gerekmez. Kaynak ve hedef komutları her
zaman **yerel** Zimbra kurulumuna karşı çalışır. Çok mailbox’lı kaynakta içerik,
hesabın `zimbraMailHost` değerine Zimbra yönetim REST portu üzerinden gidebilir;
bu SSH ile komut çalıştırmak değildir.

TOML `[source]` / `[target]` SSH ayarları reddedilir. Uzak export yalnızca
`export --target-ip` ile yapılır.

## Komutlar

`--version`, `--verbose` ve `--json-logs` üst düzey `zimigrate` komutuna aittir ve
**alt komuttan önce** yazılmalıdır:

```bash
zimigrate --verbose export --archive ./export_data
zimigrate --json-logs import --archive ./export_data
```

`./export.sh` ve `./import.sh` ek argümanların önüne `export` / `import` koyar;
sarmalayıcılara arşiv, kapsam ve SSH bayraklarını verin, `--verbose` değil.
Sarmalayıcılarla satır satır log için `ZIMIGRATE_PLAIN_OUTPUT=1` veya `TERM=dumb`
kullanın.

| Komut | Amaç |
| --- | --- |
| `./export.sh [seçenekler]` | Export (yerel Zimbra veya `--target-ip` ile SSH) |
| `./import.sh [seçenekler]` | Arşivi doğrula ve yerel Zimbra’ya aktar |
| `zimigrate export` | Kurulumdan sonra `export.sh` ile aynı |
| `zimigrate import` | Kurulumdan sonra `import.sh` ile aynı |
| `zimigrate status` | Checkpoint özetleri ve başarısız birimler |
| `zimigrate verify [--deep]` | Import etmeden arşiv dosyalarını doğrula |
| `zimigrate verify-target` | Hedefi arşivle karşılaştır |
| `zimigrate preflight` | Yerel Zimbra komutları ve sürüm |

`export`, `import`, `verify` ve `verify-target` ortak seçenekleri:

| Seçenek | Anlamı |
| --- | --- |
| `--archive DIR` | Arşiv dizini (varsayılan: `./export_data`) |
| `--config FILE` | İsteğe bağlı TOML; yoksa güvenli varsayılanlar |
| `--user EMAIL` | Bu hesap ve domaini (tekrarlanabilir) |
| `--domain NAME` | Bu domain ve hesapları (tekrarlanabilir) |

`--user` ve `--domain` virgülle ayrılmış değer kabul eder, tekrarlanabilir:

```bash
./export.sh --user a@example.com --user b@example.com
./export.sh --domain example.com,other.com
```

Yalnızca export:

| Seçenek | Anlamı |
| --- | --- |
| `--target-ip HOST` | Bu Zimbra’ya SSH; `zmprov` / `zmmailbox` çıktısı yerel arşive stream edilir |
| `--ssh-user NAME` | SSH kullanıcı adı (varsayılan: `root`) |

`verify` ayrıca `--deep` alır (her mailbox ZIP/TGZ taraması). Import bu taramayı
her zaman otomatik yapar.

`preflight` `--config FILE` ve `--side source|target|both` kabul eder (varsayılan:
`source`).

`status` yalnızca `--archive DIR` alır.

Yardım:

```bash
./export.sh --help
./import.sh --help
zimigrate --help
```

## Kullanım alternatifleri

### 1. Zimbra sunucusunda tam yerel export

Depoyu kaynak Zimbra’ya koyun, büyük bir volume’e `cd` edin:

```bash
/path/to/zimigratex/export.sh
```

TTY’de kategori menüsü çıkar. Enter tüm varsayılanları (tüm kategoriler) seçer.
Çıktı:

```text
./export_data/
```

`Ctrl+C` çalışan Zimbra komutlarını durdurur. Aynı dizinde aynı komutu yeniden
çalıştırarak devam edin.

### 2. İş istasyonundan uzak export

`ssh` olan bir makineden (Zimbra olmak zorunda değil):

```bash
./export.sh --target-ip 192.0.2.10
./export.sh --target-ip mail.example.com --ssh-user root
```

Davranış:

1. TTY’de kategori menüsü SSH’dan önce her zaman **bu** makinede çıkar. Önceki
   seçim varsayılan olarak sunulur; menü atlanmaz. Manifest yazıldıktan sonra
   farklı seçim reddedilir (yeni arşiv dizini kullanın). Zimbra menüyü göstermez.
2. SSH önce `--ssh-user` (varsayılan `root`) ile anahtar dener. Olursa parola
   sorulmaz.
3. Anahtar başarısızsa ve stdin TTY ise kullanıcı adı (varsayılan `root`) ve
   parola istenir. Parola `ssh` komut satırına yazılmaz.
4. Proses burada kalır. `zmprov` / `zmmailbox` kaynakta SSH ile çalışır.
   Mailbox arşivleri SSH stdout ile `./export_data/` altına yazılır. Kaynak
   sunucuda TGZ/ZIP oluşmaz. Boş veya başarısız stream silinir ve yeniden
   denenir.
5. Paralellik yapılandırılmış `workers` değeridir. Tam arşiv için yer **bu
   makinede** gerekir.
   Rapor: `export_data/reports/export-disk-assessment.json`.

Aynı dizinde `--target-ip` ile veya onsuz devam edin. Arşiv ilk hosta bağlıdır;
farklı `--target-ip` reddedilir.

Parola ile SSH için TTY gerekir. Etkileşimsiz uzak export için SSH anahtarı
çalışmalıdır. Uzak sunucu `zimbra` olarak veya `sudo -n -u zimbra` ile `zmprov`
çalıştırabilmelidir.

### 3. Kesilen işe devam

```bash
./export.sh
./export.sh --target-ip 192.0.2.10
./import.sh
```

Başarılı birimler atlanır; eksikler devam eder. Devam, kaynak hosta, Zimbra
sürümüne, kapsama ve export seçeneklerine bağlıdır. Checkpoint, kayıt ve bağlı
mailbox dosyaları checksum ve boyutla eşleşiyorsa yeniden kullanılır.

Ortadaki `--user` / `--domain` değişikliği için export’ta **yeni** arşiv dizini,
import’ta taze `state.sqlite3` olan bir kopya gerekir. İlk import denemesinin
politikaları checkpoint’e kilitlenir, devam sırasında sessizce değişmez.

`zimigrate` çalışırken `export_data` kopyalamayın.

### 4. Tek hesap veya tek domain

Kapsamlı çalışma kategori menüsünü atlar. Bağımlılıklar otomatik eklenir.

```bash
./export.sh --user user@example.com
./export.sh --domain example.com
./export.sh --archive ./backup_example --domain example.com
```

`--user` o hesabı, domainini, COS’unu ve (mailbox açıksa) mailbox’ını alır.
`--domain` ayrıca alias domainleri, hesapları ve dağıtım listelerini alır.

Import aynı süzgeci **tam** bir arşive uygulayabilir (bir kez export, sonra tek
mailbox geri yükleme):

```bash
./import.sh --user user@example.com
./import.sh --domain example.com
```

`--user` / `--domain` ayrıca “tüm arşiv / domain seçimi” sorusunu da atlar.

### 5. Özel arşiv dizini

```bash
./export.sh --archive /srv/migration/export_data
./import.sh --archive /srv/migration/export_data
zimigrate status --archive /srv/migration/export_data
```

Farklı kapsam, kaynak host veya export seçenekleri için ayrı dizin kullanın.

### 6. Yerel arşivi hedefe taşıma

Export **durduktan** sonra dizinin tamamını kopyalayın. İzinleri,
`manifest.json`, `state.sqlite3`, `objects/`, `mailboxes/` ve `reports/`
koruyun.

```bash
cp -a export_data /mnt/transfer/
```

Hedefte bu dizini çalışma dizinine koyun (varsayılan ad `export_data`) veya
`--archive` verin.

### 7. Etkileşimli import (yedek seçici)

Hedefte, içinde bir veya daha fazla export arşivi olan dizinde, TTY ile ve
`--archive` **olmadan**:

```bash
./import.sh
```

zimigrate `manifest.json` ve `state.sqlite3` içeren alt dizinleri listeler
(`.git`, `.venv`, `src`, `vendor` ve benzerleri atlanır). Her arşiv için
tamamlanma, kaynak host, son güncelleme, domain/hesap/liste sayıları, mailbox
verisi, kategoriler ve domain adları gösterilir. Numara seçin; Enter varsayılanı
alır (`./export_data` varsa o, yoksa listedeki ilk).

Ardından sorulur:

1. Tüm arşiv veya seçilen domain(ler) (arşivde domain varsa).
2. Kategori menüsü (yalnızca o arşivde bulunan kategoriler).

Yalnızca seçilen kapsam ve kategoriler içeri alınır.

stdin TTY değilse veya `--archive` verilmişse seçici atlanır (varsayılan
`./export_data`, `--archive` varsa o yol).

```bash
./import.sh --archive ./backup_example
```

### 8. Etkileşimsiz / betikli çalışma

TTY yoksa menü, arşiv seçici ve domain sorusu çıkmaz; config/CLI varsayılanları
kullanılır. `--user` / `--domain` yine geçerlidir.

```bash
./export.sh --archive /srv/export_data --domain example.com
./import.sh --archive /srv/export_data --domain example.com
zimigrate --verbose export --archive /srv/export_data --domain example.com
zimigrate --json-logs import --archive /srv/export_data --domain example.com
```

`status` ve `preflight` her zaman JSON basar. Export/import/verify JSON’u yalnızca
canlı panel kapalıyken basar (bkz. [Loglama ve durum paneli](#13-loglama-ve-durum-paneli)).

### 9. Durum, doğrulama ve preflight

`preflight` dışında bunlar varsayılan olarak `./export_data` kullanır:

```bash
zimigrate status
zimigrate verify --deep
zimigrate verify-target
zimigrate preflight --side source
zimigrate preflight --side target
zimigrate preflight --side both
```

- `status` — `state.sqlite3` işlem sayıları ve başarısız varlıklar.
- `verify --deep` — import’un her seferinde otomatik yaptığı arşiv doğrulaması.
- `verify-target` — hedef nesneler ile arşiv; kilitli import kategorileri ve
  eşleme politikasını checkpoint’ten okur. Import sonunda da çalışır.
- `preflight` — kurulu `zmprov` / `zmmailbox` / `zmcontrol` ve isteğe bağlı hedef
  sürüm deseni.

### 10. Disk kontrolleri

**Export:** `zmprov gqu` kullanımı, arşiv büyümesi, worker başına geçici alan ve
yedek pay. Yetersiz alan veri yazılmadan durur.
`export_data/reports/export-disk-assessment.json`.

`--target-ip` ile disk ölçümü **bu** makinededir (tam arşiv). Kaynak sunucu
mailbox staging diski olarak kullanılmaz.

**Import:** `zmvolume -l` message/index volume’leri ve geçici alan; her arşiv
üyesinin **açılmış** boyutu. Yetersiz alan durur.
`export_data/reports/import-disk-assessment.json`.

Yerel proses eşlenen uzak mailbox hostun diskini ölçemez; bu eşleme varsayılan
olarak importu durdurur. Her uzak message/index volume denetlendikten sonra
import config’de `allow_unverified_remote_capacity = true` kullanılabilir.

### 11. Başarılı import sonrası

Tamamlanmamış hesaplar `maintenance` durumunda kalır; kaynak duruma dönüş bütün
metadata ve mailbox aşamalarından sonra olur. Hesap oluşturma ve parola hash’leri
`zmprov -l` (doğrudan LDAP) ile yazılır; Zimbra `{SSHA}` değerini yeniden
hash’lemez. Ardından SOAP `zmprov fc account` çalışır; mailboxd önbelleğindeki
boş parola veya `maintenance` durumu `ldap_cache_account_maxage` (varsayılan 15
dakika) dolmadan düşer. Cache yenilemesi başarısız olursa import durur ve hesap
yeniden `maintenance` olur.

Hedef doğrulamayı sonra tekrar:

```bash
zimigrate verify-target --archive ./export_data
```

### 12. Politika için isteğe bağlı TOML

Varsayılanlar yetmezse [config.example.toml](config.example.toml) kopyalayın:

```bash
cp config.example.toml migration.toml
./export.sh --config migration.toml
./import.sh --config migration.toml --archive ./export_data
```

Config **import** davranışını değiştiriyorsa dosyayı hedefe kopyalayıp ilk
import’ta tekrar verin. Ayrıntı: [İsteğe bağlı yapılandırma](#isteğe-bağlı-yapılandırma).

### 13. Loglama ve durum paneli

Export, import, verify ve verify-target etkileşimli terminalde canlı panel
çizer (host, envanter, disk, aşama ilerlemesi). Klasik satır logları şu
durumlarda kullanılır:

- `--verbose` (alt komuttan önce)
- `--json-logs` (alt komuttan önce)
- TTY olmayan çıktı
- `TERM=dumb`
- `ZIMIGRATE_PLAIN_OUTPUT=1`

Örnek paneller (yerleşik Rich çizicisinden):

![Export dashboard](docs/screenshots/export-dashboard.svg)

![Tamamlanmış import dashboard](docs/screenshots/import-completed.svg)

Dashboard yerleşimi değişirse:

```bash
PYTHONPATH=src python scripts/generate-readme-screenshots.py
```

## Etkileşimli menüler

Yalnızca stdin TTY iken ve çalışma `--user` / `--domain` (veya kilitli devam)
ile zaten kapsamlı değilse gösterilir.

### Kategori menüsü (export ve import)

```text
Select data categories to export:
  1. Domains and alias domains [default]
  2. Classes of service (COS) [default]
  3. Accounts, passwords, resources, identities, signatures, and preferences [default]
  4. Mailbox messages and item data [default]
  5. Static and dynamic distribution lists [default]
  6. Everything except mailbox data
```

Menü metni İngilizcedir (uygulama dili).

- Enter veya `all` — kullanılabilir tüm varsayılanlar.
- Virgülle sayılar — o kategoriler.
- `6` — mailbox hariç kullanılabilir her kategori.

Bağımlılıklar otomatik eklenir:

| Seçim | Ayrıca eklenen |
| --- | --- |
| Hesaplar | Domainler, COS |
| Mailbox’lar | Hesaplar, domainler, COS |
| Dağıtım listeleri | Domainler |

Import yalnızca arşivde bulunan kategorileri sunar. Disabled satırlar seçilemez.

### Import kapsamı

```text
Select import scope:
  1. Entire archive [default]
  2. Selected domain(s)
```

`2` arşivdeki domainleri listeler; virgülle numaralar girin. Sonra kategori
menüsü o domain kümesine uygulanır.

## Export edilen veriler

- Domainler, alias domainler ve domain öznitelikleri
- Class of Service (COS) ve kaynak-hedef kimlik eşlemeleri
- Kullanıcı hesapları ve takvim kaynakları
- Parola hash’leri, alias’lar, tercihler, filtreler ve yönlendirmeler
- Kimlikler, imzalar ve desteklenen haricî veri kaynakları
- Statik ve dinamik dağıtım listeleri, alias’ları, öznitelikleri ve statik üyeleri
- Postalar, takvimler, kişiler, görevler ve Briefcase (Zimbra REST ZIP/TGZ)
- `zimbraACE` içindeki taşınabilir kaynak UUID’lerinin hedef UUID’lerine eşlenmesi

Canlı kimlik doğrulama token’ları export edilmez. Sistem hesaplarının metadata’sı
arşivlenir; sistem mailbox içerikleri ve hedef servis kimlikleri varsayılan olarak
aktarılmaz. Global ve sunucu LDAP ayarları arşivlenmez ve uygulanmaz (hostname,
sertifika, sunucu kimliği, port, LDAP/MTA topolojisi).

Zimbra dört veri kaynağı credential alanını kaynak `zimbraDataSourceId` değerine
bağlı kodlar. Export bunları proses içinde çözer, plaintext’i arşive yazar; hedef
yeni veri kaynağı ID’si ile yeniden şifreler. Import sırasında veri kaynağı bütün
öznitelik ve credential’lar uygulanana kadar kapalı kalır. LDAP ciphertext’ini
doğrudan kopyalamak kullanılamayan credential üretir.

## Import öncesi doğrulama

`zimigrate import` hedefte değişiklik yapmadan önce şunların hepsini yapar:

- tamamlanmış, desteklenen `manifest.json`;
- özgün `state.sqlite3` mevcut ve okunabilir;
- her domain, COS, hesap, kaynak ve liste kaydı okunabilir;
- her provisioning kaydı checkpoint SHA-256 ile eşleşir;
- manifest nesne sayıları diskteki dosyalarla eşleşir;
- her mailbox dosyası kayıtlı boyut ve SHA-256 ile eşleşir;
- referansı olmayan nesne/mailbox dosyası yoktur;
- her ZIP/TGZ açılabilir, bozulmamış ve güvenli üye yollarına sahiptir;
- bu hostta gereken her `zmprov` / `zmmailbox` komutu vardır.

Biri başarısızsa hedef değişmez. Düzeltme veya yeniden kopyadan sonra import’u
tekrar çalıştırın.

## Gereksinimler

- Python 3.11+ ve `rich` (veya sarmalayıcı / `vendor` yolu);
- kurulu Zimbra sürümünün desteklediği 64-bit x86_64 glibc Linux;
- **yerel** export/import: `/opt/zimbra/bin/zmprov`, `zmmailbox`, `zmcontrol`,
  `zmhostname`; `zimbra` kullanıcısı veya `sudo -n -u zimbra`;
- **uzak** export: yerelde `ssh`; Zimbra tarafında yine yukarıdaki komutlar,
  `zimbra` veya `sudo -n -u zimbra` ile;
- iş istasyonu/arşiv için yeterli alan ve worker başına bir şifresiz mailbox
  parçası kadar geçici alan;
- `zmprov` çalışan makinede preflight’tan geçen yerel Zimbra FOSS.

## İsteğe bağlı yapılandırma

Dosya olmadan varsayılanlar: tüm normal hesaplar, mailbox içeriği ve görünen
secret hash’ler, sekiz worker, mevcut nesnelerde merge, mailbox çakışmasında
skip, sıkı öznitelikler, REST `meta=1` ve `lock=1`.

```bash
cp config.example.toml migration.toml
```

Yararlı `[transfer]` anahtarları:

| Anahtar | Varsayılan | Not |
| --- | --- | --- |
| `workers` | `8` | 1–64 |
| `retries` | `3` | Yalnızca geçici komut hataları |
| `retry_base_seconds` | `1.0` | Üstel bekleme tabanı |
| `include_*` | `true` | Kategoriler; TTY’de menü geçersiz kılar |
| `include_system_mailboxes` | `false` | Açmak tehlikeli olabilir |
| `include_secrets` | `true` | Parola hash’leri ve veri kaynağı secret’ları |
| `account_include` / `account_exclude` | `["*"]` / `[]` | fnmatch desenleri |
| `target_users` / `target_domains` | `[]` | Tercihen CLI `--user` / `--domain` |
| `mailbox_mode` | `"full"` | Veya `"year-chunks"` |
| `mailbox_format` | `"zip"` | Eski REST için `"tgz"` |
| `mailbox_lock` | `true` | Zimbra `lock=1` reddederse export durur |
| `mailbox_start_year` | `1970` | Yıl parçası başlangıcı |
| `mailbox_chunk_years` | `5` | Yıl parçası genişliği |

Yıl parçaları locale tarih değil, sayısal UTC epoch milisaniye (`date:<` /
`date:>=`) kullanır ve çakışmaz. `mailbox_conflict_resolution = "reset"` ise
yalnızca ilk parça mailbox’ı sıfırlar; sonrakiler önceki parçayı silmemek için
`skip` kullanır. En eksiksiz kopya `mailbox_mode = "full"`. REST sorgu export’u
Zimbra arama gibi boş klasörleri ve aranabilir tarihi olmayan öğeleri atlayabilir.

`mailbox_lock = false` yalnızca denetimli bakım penceresinde.

Yararlı `[import]` anahtarları:

| Anahtar | Varsayılan | Not |
| --- | --- | --- |
| `expected_target_version_pattern` | `""` | Boş = preflight geçen her sürüm |
| `existing_policy` | `"merge"` | `merge`, `skip` veya `fail` |
| `mailbox_conflict_resolution` | `"skip"` | `skip`, `modify`, `replace` veya `reset` |
| `strict_attributes` | `true` | Şema reddi importu durdurur |
| `import_system_accounts` | `false` | Bilinçli değilse kapalı tutun |
| `allow_unverified_remote_capacity` | `false` | Disk kontrollerine bakın |
| `default_mailhost` | yok | Çok mailbox’lı hedef |
| `[import.mailhost_map]` | boş | Eski hostname → yeni hostname |

`strict_attributes = false` yalnızca `reports/import-warnings.ndjson` incelendikten
sonra. Servis, bağlantı ve timeout hataları asla öznitelik uyarısına indirgenmez.

`[source]` / `[target]` `zimbra_user`, komut/mailbox zaman aşımları ve yönetim
REST şema/port ayarlayabilir. `mode = "ssh"` ve SSH anahtarları reddedilir.

Kaldırılmış anahtarlar (`include_global_config`, `apply_global_config`,
`server_map`, arşiv şifreleme anahtarları ve benzeri) yok sayılmaz; yapılandırma
hatası verir.

`command_timeout_seconds` varsayılanı 300, `mailbox_timeout_seconds` 14400.

## Güvenlik ve güvenilirlik

Arşiv provisioning kayıtları ve mailbox yükleri için SHA-256, atomik yazım,
`0600` dosyalar, `0700` dizin ve zorunlu SQLite checkpoint kullanır. Kayıtlar ve
mailbox içerikleri düz metindir. Worker gönderimi sınırlıdır. Yalnızca sınıflandırılmış
geçici hatalar sınırlı üstel yeniden deneme alır. Geçici mailbox parçaları
`export_data/.tmp` altındadır ve sonra silinir. Hassas `zmprov` değerleri proses
argümanı değil stdin batch’tir.

Mailbox protokolü Zimbra’nın
[REST export/import referansını](https://github.com/Zimbra/zm-mailbox/blob/develop/store/docs/rest.txt)
izler. `zmprov`, `zmmailbox` ve cache için dayanak
[resmî komut satırı kılavuzudur](https://github.com/Zimbra/adminguide/blob/develop/cmdlineutils.adoc).

Dizini güvenilir, tercihen disk şifrelemeli depolamada tutun. Kopyalanan arşiv
hesap adları, secret export açıksa parola hash’leri, mailbox içeriği ve checkpoint
metadata içerir. Ayrıntı: [SECURITY.md](SECURITY.md).

## Bilinen sınırlar

- Bu uygulama seviyesinde bir geçiştir; LDAP, MariaDB ve blob store’un birebir
  fiziksel geri yüklemesi değildir.
- Sertifikalar, özel anahtar dosyaları, `zmlocalconfig`, işletim sistemi paketleri,
  MTA kuyruğu, DNS, firewall ve ticari Network Edition backup kurulmaz. Hesap
  `jpegPhoto`, `userCertificate` ve `userSMIMECertificate` LDAP ikili
  öznitelikleridir; `zmprov` argv ile DER/JPEG yazamaz, import atlar.
- Varsayılan imza kimlikleri hedef imzalar oluşturulduktan sonra yeniden eşlenir.
  `zimbraPrefMailSignatureContactId` bir kişi UUID’sidir ve hedefte boş bırakılır.
- Cross-mailbox paylaşım kimlikleri değişebilir. Paylaşımlar ve delege klasör
  yetkileri geçişten sonra kontrol edilmeli, gerekirse yeniden oluşturulmalıdır.
- Hedef doğrulaması provisioning durumunu ve başarılı mailbox REST import
  checkpoint’lerini karşılaştırır; öğe öğe içerik karşılaştırması yapmaz.
- Kaynak export sırasında değişmeye devam ederse küme genelinde işlemsel snapshot
  oluşmaz. Son geçiş bakım veya yazma dondurma penceresinde yapılmalıdır.
- Import disk hesabı sıkıştırılmış ZIP/TGZ değil, açılmış üye boyutunu kullanır.
  Eşlenen uzak mailbox hostlar, işletici her hostu denetleyip sınırı kabul etmedikçe
  reddedilir.
- Hedef sürüm kilidi isteğe bağlıdır. `zmprov` / `zmmailbox` / `zmcontrol`
  çalıştıran her yerel sürüm kabul edilir; belirli `zmcontrol -v` için
  `import.expected_target_version_pattern` ayarlanır.

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
Affero Genel Kamu Lisansı’nın yalnızca 3. sürümü altında yeniden dağıtabilir
ve değiştirebilirsiniz. Ayrıntılar için [LICENSE](LICENSE) dosyasına bakın.
