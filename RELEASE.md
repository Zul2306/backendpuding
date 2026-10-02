# Rilis backend dan aplikasi

Kontrak rilis saat ini: API 2.2.0, contract_version 1, schema_revision
20261002_prediction_execution. Sumber kontrak: app/release_contract.json;
frontend/lib/services/release_contract.dart harus sesuai saat build.

## Urutan rilis

1. Jalankan tes backend, compileall, flutter analyze, dan flutter test.
2. Buat backup database server tujuan dan verifikasi file backup sebelum migrasi.
3. Periksa schema aktual, lalu terapkan hanya migrasi yang belum dijalankan
   dengan persetujuan pengelola. SQL tersedia di migrations/. Jangan mengisi
   harga historis yang tidak diketahui dengan harga terkini.
4. Deploy backend dari artefak rilis yang sama dan restart seluruh worker.
5. Dari direktori b, periksa URL yang benar-benar digunakan aplikasi:

   ```powershell
   python scripts/check_release.py --base-url https://server-anda --output release-check.json
   if ($LASTEXITCODE -ne 0) { throw 'Rilis gagal diperiksa; jangan distribusikan aplikasi.' }
   ```

   Pemeriksaan hanya GET /release/readiness dan /openapi.json. Ia memeriksa
   versi artefak, kontrak, endpoint beserta metode HTTP, field wajib request,
   dan schema database aktual. Status keluar 0 berarti lulus, 1 berarti gagal.
   Simpan laporan bersama ID commit/build rilis. Periksa setiap instance jika
   server memakai beberapa instance; pemeriksaan satu URL tidak menjamin
   seluruh worker di belakang load balancer memakai versi yang sama.
6. Setelah pemeriksaan lulus, build aplikasi dengan URL server yang sama:

   ```powershell
   flutter build apk --dart-define=API_BASE_URL=https://server-anda
   ```

   Untuk memastikan pemeriksaan selalu mendahului build, jalankan dari
   workspace (script memakai URL yang sama untuk pemeriksaan dan build):

   ```powershell
   .\frontend\tool\build_checked_release.ps1 -ApiBaseUrl https://server-anda
   ```

   APK tidak dibuat bila pemeriksaan gagal. Jika Python/Flutter tidak tersedia
   di PATH, gunakan parameter -PythonCommand dan -FlutterCommand dengan path
   executable yang benar. Laporan disimpan di frontend/build/release-check.json.

7. Uji aplikasi staging: pembacaan harga, laporan, dan preview produksi; uji
   penyimpanan hanya dengan data uji yang disetujui. Distribusikan setelah lulus.

Aplikasi memeriksa kesiapan sebelum membuka menu operasional. Jika server lama
mengembalikan 404 atau schema belum sesuai, menu belum dibuka dan pengguna
mendapat tombol Coba Lagi. Pemeriksaan tidak membuat data atau migrasi otomatis.
GET /health tetap menandakan proses hidup, bukan kesiapan schema.

## Bila gagal

Jangan distribusikan aplikasi baru. Lengkapi migrasi/backend yang hilang dan
jalankan pemeriksaan ulang. Pertahankan backup; jangan otomatis menghapus
kolom atau mengembalikan backup di atas transaksi baru. Koordinasikan rollback
backend dan aplikasi agar kontraknya tetap sesuai.

Versi 2.2.0 menambah query pagination (page/limit), filter tanggal transaksi, dan metadata pagination. Tidak memerlukan migrasi database baru.
