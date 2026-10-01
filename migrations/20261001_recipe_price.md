# Harga per resep

Schema aktual diperiksa: resep belum memiliki harga_jual_per_potong.

1. Backup database sebelum perubahan.
2. Jalankan 20261001_recipe_price.sql sekali pada database aplikasi.
3. Restart backend, lalu isi harga melalui Kelola Resep > buka resep > Atur Harga.

Tidak menghapus data atau tabel. Harga awal 0 berarti belum diatur. Penjualan harian memakai harga resep prediksi aktif; bila belum diisi tetap Rp3.000. Histori transaksi tidak dihitung ulang saat mengubah harga. Jika ada beberapa resep prediksi aktif, sistem memakai resep dengan id terkecil; input harian saat ini belum memisahkan jumlah per jenis puding.
