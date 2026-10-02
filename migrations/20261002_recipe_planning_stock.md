# Jenis puding berdasarkan resep

Schema MySQL diperiksa 2 Oktober 2026: resep belum memiliki metode_perencanaan,
stok_batch belum memiliki id_resep. Empat resep aktif: Marie (prediksi), Lumut,
Chococheese, Snowcake (manual). Ada delapan batch lama tanpa identitas resep.

1. Backup database aplikasi sebelum menjalankan SQL.
2. Hentikan sementara penulisan produksi dan edit resep selama deployment.
3. Jalankan 20261002_recipe_planning_stock.sql sekali. MySQL DDL tidak bisa
   dibatalkan dengan rollback. Jika deployment parsial, periksa SHOW COLUMNS
   dan SHOW CREATE TABLE sebelum mengulang; jangan jalankan ALTER yang sudah ada.
4. Deploy/restart backend dan frontend secara bersamaan. Produk lama serta
   relasi id_produk tetap dipertahankan untuk histori dan kompatibilitas.
5. Verifikasi GET /resep/manage?metode=manual&status=aktif menampilkan semua
   resep manual. GET /produksi/manual/preview memakai id_resep, bukan id_produk.
6. Verifikasi GET /stok-puding dan tampilan Stock Opname > Stok Puding per Jenis.

Batch baru selalu menyimpan id_resep. Produksi manual memasukkan seluruh hasil
ke batch jenis tersebut. Produksi prediksi memasukkan sisa setelah pemenuhan
kebutuhan ke batch resep prediksi; FIFO hanya mengambil batch resep yang sama.
Batch lama dibiarkan NULL, ditampilkan Belum ditentukan, dan tidak digunakan
untuk produksi prediksi sebelum identitasnya dikonfirmasi pengguna. Tidak ada
penambahan stok dari produksi historis (menghindari stok fiktif/ganda).

Metode perencanaan sekarang milik resep. Produk tidak lagi menjadi pilihan
jenis puding pada form. API lama id_produk untuk preview diterima hanya jika
tepat satu resep manual aktif cocok; bila ada banyak resep ditolak 400.

Prediksi/input harian belum dibedakan per jenis; sistem mendukung satu resep
prediksi aktif. Menambah/mengubah resep prediksi kedua ditolak 409. Mengubah
metode tidak mengubah identitas batch atau histori produksi yang sudah tercatat.
