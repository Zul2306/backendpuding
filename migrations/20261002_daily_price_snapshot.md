# Harga pada saat input harian

Schema aktual diperiksa: data_harian belum memiliki harga_jual_per_potong.

1. Backup database.
2. Hentikan sementara penulisan input harian selama deployment.
3. Jalankan 20261002_daily_price_snapshot.sql sekali.
4. Deploy/restart backend. Frontend tidak memerlukan perubahan untuk fitur ini.
5. Pastikan input baru menyimpan harga resep prediksi (Puding Marie) saat input
   disimpan; bila harga resep belum diisi memakai fallback Rp3.000.

Harga baru hanya dipakai untuk input berikutnya. Setiap baris mitra menyimpan
harga sendiri; perubahan harga di tengah hari tidak mengubah baris yang sudah
tersimpan. Batch mengambil satu harga untuk seluruh baris dalam batch tersebut.
Agregasi penjualan dan return memakai harga masing-masing baris, bukan master
harga terbaru. db.flush dilakukan sebelum agregasi agar baris baru ikut dihitung.

GET /keuangan tidak menghitung ulang atau menulis transaksi. Nominal transaksi
lama tetap dipertahankan. Harga lama NULL tidak diisi otomatis karena harga
historis tidak tersedia. Bila mencoba menambah input ke tanggal yang mempunyai
baris lama bernilai nonzero tanpa harga historis, penyimpanan ditolak 409 dan
rollback, supaya nominal lama tidak tertimpa. Harga historis dapat dipulihkan
hanya dari bukti yang dikonfirmasi pengguna, bukan dari harga saat ini.

Perubahan ini tidak dapat memulihkan nominal yang sudah berubah sebelum
perbaikan; pemulihan memerlukan bukti transaksi/backup dan persetujuan terpisah.
