-- Jalankan sekali setelah backup dan pemeriksaan schema aktual.
ALTER TABLE data_harian
  ADD COLUMN harga_jual_per_potong DECIMAL(15,2) NULL;
-- NULL untuk data lama: harga pada saat input tidak diketahui.
-- Tidak mengubah nominal transaksi lama dan tidak mengisi harga lama dengan harga sekarang.
