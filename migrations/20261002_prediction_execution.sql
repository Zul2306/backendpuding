-- Jalankan setelah backup dan persetujuan perubahan schema.
CREATE TABLE eksekusi_prediksi (
  id_permintaan VARCHAR(36) NOT NULL,
  id_rencana INT NOT NULL,
  tanggal_target DATE NOT NULL,
  sidik_permintaan VARCHAR(64) NOT NULL,
  hasil JSON NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id_permintaan),
  UNIQUE KEY uq_eksekusi_target (tanggal_target),
  CONSTRAINT fk_eksekusi_rencana FOREIGN KEY (id_rencana)
    REFERENCES prediksi_harian (id_prediksi)
) ENGINE=InnoDB;
