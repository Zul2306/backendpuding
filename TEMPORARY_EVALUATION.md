# Evaluasi model sementara

Fitur opsional, default nonaktif. Tidak memerlukan migrasi, tidak membuat record
prediksi/produksi, dan tidak mengubah data laporan, stok, atau keuangan.

## Mengaktifkan

Backend (PowerShell, dari direktori `b`):

```powershell
$env:ENABLE_TEMP_EVALUATION = 'true'
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Untuk server yang sudah berjalan, atur environment pada proses/service tersebut
dan restart. Jangan menjalankan dua server pada port yang sama.

Frontend Windows (dari direktori `frontend`):

```powershell
flutter run -d windows --dart-define=ENABLE_TEMP_EVALUATION=true --dart-define=API_BASE_URL=http://192.168.100.15:8000
```

Windows memerlukan Visual Studio dengan workload **Desktop development with C++**
beserta komponen defaultnya; periksa menggunakan `flutter doctor -v`.
Hentikan aplikasi dan jalankan ulang setelah perubahan native, bukan hot reload.
Halaman memeriksa kontrak endpoint evaluasi sebelum tombol Jalankan aktif.

Menu **Prediksi > Evaluasi Model Sementara** independen dari kunci input hari ini
dan status konfirmasi produksi. Server versi lama/default nonaktif menampilkan
pesan bahwa evaluasi belum tersedia; aplikasi tidak menjalankan alur produksi.

## Pemakaian

1. Default periode uji 21–27 September 2026; batas data latih 20 September 2026
   mengikuti keterangan pengguna. Tanggal terakhir data latih adalah tanggal
   **target/label terakhir** yang dilihat model, bukan tanggal file dibuat.
2. Pilih semua mitra atau satu mitra. Semua mencakup mitra yang kini nonaktif
   karena laporan historisnya tetap relevan; tanpa model/laporan akan dilewati.
3. Konfirmasi bahwa target pelatihan adalah **terjual besok**. Pengguna sudah
   menjelaskan bahwa nama lama `target_suplai_besok` berisi terjual besok.
   Pernyataan ini dicatat di hasil; fitur tidak mengubah metadata/model asli.
4. Jalankan. Maksimal 31 hari, berakhir sebelum hari ini. Model aktif saat
   permintaan digunakan tetap untuk setiap mitra selama evaluasi; tidak melatih ulang.
5. Tekan **Unduh CSV**, pilih lokasi melalui dialog **Save As** Windows, lalu
   Simpan. Pembatalan dialog tidak menampilkan pesan berhasil. Kegagalan tulis
   menampilkan pesan untuk mencoba lokasi lain. CSV memakai UTF-8 BOM agar
   nama mitra terbaca di Excel. File mencakup tanggal target, prediksi, aktual
   pada tanggal target yang sama, selisih, metrik, ID/SHA256 model dan peringatan.
   Simpan sebelum menutup halaman; hasil tidak disimpan ke database.
   Di Android tombol memakai dialog dokumen bawaan. Ekspor belum tersedia di web.

## Metode dan batas interpretasi

- Untuk target H, input model berasal dari fitur kausal baris H-1. Riwayat sampai
  H-1 boleh termasuk aktual hari sebelumnya dalam periode pengujian. Pembentukan
  fitur dilakukan sekali per mitra untuk efisiensi menggunakan fungsi produksi
  yang memiliki lag/rolling ke belakang; prediksi tidak menerima baris H atau sesudahnya.
- Prediksi H dibandingkan dengan terjual aktual H. Baseline adalah prediksi
  pembanding (terjual H-1), bukan jawaban aktual dan bukan dasar MAE model.
- Terjual dihitung dari `jumlah_suplai - jumlah_return`, termasuk nol yang valid.
  Angka negatif/return melebihi suplai, duplikat, laporan target yang belum ada,
  atau laporan H-1 yang belum ada tidak dibuatkan angka nol pengganti.
- Riwayat yang memiliki jeda lebih lama tetap mengikuti fitur produksi berbasis
  urutan laporan (lag/rolling jumlah baris), tanpa mengisi tanggal kosong.
- Skor memakai keluaran model nonnegatif sebelum pembulatan, bukan hasil edit
  pengguna, suplai rekomendasi, atau jumlah produksi. MAE, RMSE, bias, dan MAE
  baseline terjual kemarin dihitung pada pasangan mitra/tanggal valid yang sama.
  Agregat dihitung dari semua pasangan valid, bukan rata-rata MAE mitra.
- Metadata `tanggal_train_akhir`/`tanggal_test_akhir` dianggap tanggal fitur;
  ditambah satu hari secara konservatif untuk label besok. Jika mencapai periode
  uji, mitra dilewati karena risiko kebocoran (termasuk tuning pada set test).
  Metadata tidak lengkap/target tidak dikenal ditandai belum terverifikasi.
  Batas latih yang dimasukkan pengguna adalah deklarasi, bukan bukti pelatihan.
- Perbedaan keterangan pengguna vs metadata ditampilkan apa adanya. Fitur tidak
  menganggap tanggal pembuatan file sebagai akhir data latih. Model dari folder
  lain yang sesuai perlu dipasang melalui alur pengelolaan model yang berlaku.
- Skor mengevaluasi **penjualan tercatat**, bukan permintaan yang tidak terpenuhi
  ketika stok habis. Satu minggu hanya memberikan evaluasi periode singkat.

## Menonaktifkan atau menghapus

- Backend: hapus environment `ENABLE_TEMP_EVALUATION` atau set `false`, restart.
  Kedua endpoint evaluasi merespons 404 saat nonaktif.
- Frontend: build ulang tanpa `--dart-define=ENABLE_TEMP_EVALUATION=true`.
  Tombol evaluasi tidak tampil pada build normal.
- Untuk membuang kode: hapus import/include router di `app/main.py`, file
  `app/temporary_evaluation.py`, import/tombol kondisional di
  `prediction_screen.dart`, serta `temporary_evaluation_screen.dart` dan
  `services/evaluation_csv.dart`. Hapus juga handler ekspor di MainActivity
  Android serta `windows/runner/evaluation_export.*` dan registrasi channel/link
  comdlg32 pada runner Windows. Hapus dua file test khusus bila fitur dibuang.
  Tidak ada data atau schema database yang perlu dihapus.
