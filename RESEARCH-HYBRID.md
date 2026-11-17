# Riset: Software AI → Blender (Hybrid Headless + Live)

Status: riset bersama, 2026-11-17. Belum ada kode produksi.
Tujuan: software baca prompt + gambar/video user, panggil API AI (Gemini / Muse / DeepSeek / 9router),
lalu bangun model 3D dengan mengontrol Blender secara langsung. Jalan di Windows (laptop) dan Oracle VPS.

## Keputusan yang sudah dibuat

1. Jalur utama = LLM via API → script Blender Python (bpy) → Blender. TIDAK butuh model AI lokal.
   Model image-to-3D lokal (TripoSR, Hunyuan3D, TRELLIS) hanya opsi tambahan, bukan andalan.
2. Executor HYBRID (pilihan user 2026-11-17): headless + live, bisa ditonton.
3. Blender 5.2.0 LTS sebagai versi patokan (sama dengan Blender Windows user dan runtime ini).

## Peta hardware

| Mesin | Spek | Cocok untuk | Tidak cocok untuk |
|---|---|---|---|
| Laptop LOQ 15IAX9 | i5-12450HX 8C/12T, RTX 3050 6GB GDDR6, RAM varian ? (8/12/16/24GB, maks 32GB) | Live Blender + headless + GPU lokal ringan (TripoSR) + 3ds Max/Inventor (nanti) | TRELLIS.2 full (24GB), Hunyuan3D 2.1 full (29GB) |
| VPS Oracle | 2 core ARM Ampere, tanpa GPU, Ubuntu, Hermes + 9router sudah jalan | Otak API, antrean job, headless Blender CPU, CadQuery | Model 3D GPU apa pun; render berat |

Catatan ARM: Blender resmi hanya menyediakan build Linux x86-64, tidak ada linux-arm64.
Di VPS, Blender hanya lewat paket distro/snap (APT Ubuntu 24.04 = 4.0.2, snap = 5.2.x).
Wheel `bpy` juga tidak ada untuk Linux aarch64. Masih harus dites langsung di VPS.

## Arsitektur hybrid

```
[Upload: prompt + gambar/video]
        |
[Provider AI]  9router / Gemini / Muse / DeepSeek (API cloud, ganti-ganti)
        |  1) vision: ekstrak spec JSON (ukuran, fitur, jumlah lubang)
        |  2) codegen: tulis script bpy dari spec
        v
[Job queue + job dir per job: tmp/jobs/<job_id>/]
        |
   +----+----+
   |         |
[Headless] [Live]   <-- satu kontrak hasil yang sama
   |         |
[Verifikasi: probe geometri + render 6 sisi -> vision cek -> revisi script, ulang]
        |
[Output: .blend + .STL/.OBJ + preview + laporan ukuran]
```

Pembagian kerja (ini pola yang direkomendasikan riset: jangan pilih satu, pakai keduanya
untuk pekerjaan yang berbeda):

- Headless: build berat / batch / antrean / semua job VPS. Satu job = satu proses Blender
  segar, gagalnya terisolasi, tidak merusak sesi kerja user.
- Live: nudge kecil + iterasi interaktif di laptop, user menonton viewport berubah.
  Job berat tetap dilempar ke headless, hasilnya dibuka di sesi live.

## Kontrak executor headless (Windows + VPS + runtime ini)

- Perintah: `blender -b --python script.py -- <argumen job>` (argumen setelah `--`,
  dibaca dari `sys.argv`). Selalu argument list, `shell=False`, jangan pernah rangkai
  string shell. Timeout wajib per job; kalau habis: SIGTERM, grace period, lalu SIGKILL.
- Satu job = satu direktori kerja terisolasi. Output tidak pernah ditulis ke root proyek.
- Jangan jalankan Blender sinkron di dalam thread request web (FastAPI). Job jalan di
  worker/antrean, UI hanya polling status.
- Script bpy harus self-contained: hapus objek default, set unit mm, tulis laporan JSON
  (nama objek, jumlah vertex/face, bounding box, daftar file output) + render preview
  resolusi rendah (Cycles CPU, sample kecil — EEVEE gagal headless tanpa libEGL).
- Blender 5.2.0 LTS di runtime ini sudah terverifikasi: `blender --version` = 5.2.0 LTS,
  build 2026-07-14. Pola headless bpy + export + render CPU sudah terbukti di proyek
  Columbus / Datamars / Advantage.

## Kontrak executor live (laptop Windows saja)

Pola wajib — ini aturan keras Blender, bukan preferensi:

- `bpy` TIDAK thread-safe. Semua akses `bpy.data` / `bpy.context` / `bpy.ops` HANYA
  boleh dari main thread Blender. Panggil dari thread lain = crash (access violation / segfault).
- Addon di dalam Blender menjalankan TCP server socket di thread terpisah (localhost saja).
  Thread socket hanya menerima + parsing perintah JSON. Eksekusi dijadwalkan ke main thread
  lewat `bpy.app.timers.register(wrapper, first_interval=0.0)`. Ini pola yang dipakai
  blender-mcp (port 9876) dan LL3M (port 8888).
- Protokol: JSON per perintah: `{"id":..., "token":..., "type":"execute_code"|"get_scene"|"screenshot", "code":...}`.
  Balasan: `{"id":..., "ok":..., "stdout":..., "error":..., "result":...}`. stdout ditangkap
  dengan redirect di dalam eksekusi.
- Keamanan live: bind `127.0.0.1` saja, JANGAN `0.0.0.0`, jangan expose port. Token rahasia
  per sesi (addon tanpa auth = proses apa pun di mesin bisa menjalankan Python sebagai user).
  Selalu SAVE + kerja di COPY dulu sebelum menjalankan kode hasil AI. Matikan server saat selesai.
- Batasan: kalau user membuka file .blend lain di tengah sesi, operator/modal yang berjalan
  bisa ikut batal. Setelah `open_mainfile`, koneksi live harus dianggap putus dan dimulai ulang.
- Bedakan dari percobaan MCP 28 Okt 2026 (DITINGGALKAN user): waktu itu pengontrolnya Muse
  dari cloud sehingga butuh supergateway + tunnel Cloudflare. Di software ini pengontrolnya
  jalan di mesin yang sama dengan Blender, jadi socket lokal saja, tanpa tunnel.

## Alur verifikasi (kunci akurasi TD)

Tanpa loop ini, sekali-jadi hampir pasti ada miss (pengalaman proyek botol: side view
sering lolos dari cek front view saja). Setiap iterasi wajib:

1. Probe geometri dari file hasil (bukan dari render): bounding box, jumlah lubang
   tembus (raycast dua arah), boundary edges = 0 untuk solid tertutup, ukuran fitur kunci.
2. Render ortho 6 sisi resolusi rendah + viewport screenshot (mode live).
3. Vision LLM membandingkan render vs referensi + angka probe vs spec TD.
4. Kalau miss: revisi HANYA bagian yang salah, buktikan bagian lain tidak berubah
   (aturan proyek Advantage: posisi yang sudah disetujui tidak boleh bergeser).

## Keamanan eksekusi kode AI

Panduan backend produksi bilang "jangan pernah mengeksekusi kode Python mentah hasil LLM".
Untuk tool pribadi single-operator ini, kode bpy penuh memang dibutuhkan (model TD seperti
Datamars tidak bisa diungkapkan dengan DSL perintah tetap). Kompensasinya:

- Lokal saja, single user, tanpa endpoint publik. API key di file chmod 600 / vault,
  tidak pernah di chat atau di log.
- Headless: proses terisolasi per job + timeout + direktori kerja terpisah.
- Live: localhost + token + copy-first + user bisa menghentikan kapan saja.
- Opsi "tinjau dulu sebelum jalan" untuk kode live: user melihat diff/script sebelum dieksekusi.
- Kode hasil AI dilarang mengakses jaringan dan menulis di luar direktori job
  (dicek dengan pemindaian teks sederhana sebelum eksekusi; bukan sandbox sempurna,
  tapi cukup untuk pemakaian pribadi).

## Jalur lain (nanti, bukan MVP)

- CadQuery/build123d (Python + Open CASCADE): headless murni CPU, ekspor STEP/STL,
  jalan di Windows dan ARM. Kandidat executor kedua untuk part prismatik presisi.
- Mesh instan lokal (laptop saja): TripoSR (~6GB VRAM, MIT, kualitas blockout),
  Hunyuan3D-2mini (~5GB). TRELLIS.2 full dan Hunyuan3D 2.1 full tidak muat di 6GB.
- Cloud mesh API (Meshy/Tripo/Rodin, ~$20/bulan): cadangan saja, bukan andalan.
- 3ds Max (MaxScript/Python) dan Inventor (COM/iLogic): Windows-only + lisensi,
  jembatan terpisah setelah Blender stabil.

## Rencana tes POC (urut)

1. POC-1 headless: script bpy kubus/plat berlubang → `blender -b`, cek laporan JSON +
   export STL + render. Tes di runtime ini dulu (gratis, Blender 5.2.0 siap).
2. POC-2 provider: satu prompt + satu gambar → spec JSON → script bpy → headless →
   verifikasi. Pakai 9router dulu (key sudah ada di VPS), fallback Gemini/DeepSeek.
3. POC-3 live: addon socket minimal di laptop user (echo + execute_code + screenshot),
   tes dari script Python lokal. User menonton viewport.
4. POC-4 VPS: cek Blender snap/apt ARM + satu job headless end-to-end di VPS.

## Pertanyaan terbuka

- RAM laptop user berapa GB? (menentukan trik low-VRAM dan ukuran job paralel)
- Sisa SSD laptop berapa? (model lokal + cache Blender bisa puluhan GB)
- UI: web lokal (FastAPI + browser) atau aplikasi desktop dulu? Rekomendasi: web lokal,
  satu kode untuk laptop dan VPS.
- Provider pertama: 9router (sudah ada) atau langsung Gemini API? Rekomendasi: 9router
  sebagai default + fallback berantai.

## Sumber riset (dibaca 2026-11-17)

- Blender MCP thread safety / bpy.app.timers: DeepWiki blender-mcp socket server,
  DeepWiki LL3M Blender integration, linux-graphics-stack-book ch.244.
- Headless subprocess best practices: AnimationStudio blender-automation SKILL,
  ComfyUI_Blender_toolbox AI workflow research (rekomendasi hybrid dua paradigma).
- TRELLIS.2 / Hunyuan3D 2.1 / TripoSR VRAM: README + panduan instalasi resmi
  (24GB / 10+21GB / ~6GB).
- Blender ARM64: tidak ada build resmi linux-arm64; opsi distro/snap; tidak ada wheel bpy aarch64.
