# Perlindungan eksekusi produksi prediksi

1. Backup database dan hentikan sementara input/produksi saat deployment.
2. Setelah persetujuan, jalankan `20261002_prediction_execution.sql` sekali.
3. Deploy backend dan frontend bersama. Klien lama tanpa ID rencana, tanggal target, dan UUID ditolak validasi 422.
4. GET preview memberikan `id_rencana`: ID prediksi terkecil dari anggota aktif pada tanggal target; tanggal mengidentifikasi kelompok prediksi mitra.
5. POST eksekusi mengunci anggota rencana, memvalidasi ID/tanggal/jumlah/status, lalu menyimpan hasil permintaan bersama stok, produksi, mutasi dan status dalam satu transaksi.
6. UUID sama dan payload sama mengembalikan hasil sebelumnya. UUID sama dengan payload berbeda ditolak 409. Rencana/tanggal yang selesai tidak dapat dijalankan dengan UUID baru.
7. Tidak ada backfill. Prediksi lama berstatus diproduksi tetap ditolak; riwayat stok dan keuangan tidak diubah.
8. Endpoint alternatif `/prediksi/produksi` memakai perlindungan yang sama; `/produksi` hanya menerima produksi manual.
