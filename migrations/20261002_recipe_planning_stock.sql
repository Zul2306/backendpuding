-- Jalankan sekali setelah backup dan pemeriksaan schema.
-- Tidak menghapus data/tabel. DDL MySQL melakukan implicit commit.
ALTER TABLE resep
  ADD COLUMN metode_perencanaan ENUM('prediksi','manual') NOT NULL DEFAULT 'manual';
UPDATE resep r JOIN produk p ON p.id_produk = r.id_produk
  SET r.metode_perencanaan = p.metode_perencanaan;
ALTER TABLE stok_batch
  ADD COLUMN id_resep INT NULL,
  ADD INDEX ix_stok_batch_id_resep (id_resep),
  ADD CONSTRAINT fk_stok_batch_resep FOREIGN KEY (id_resep) REFERENCES resep(id_resep);
-- Batch lama tetap id_resep=NULL; tidak boleh ditebak atau dicampur ke FIFO.
