-- Круглосуточная запись камер на сервере площадки (deploy/truenas/video.sh): адрес камеры и её логин/пароль для
-- видеопотока (RTSP). Нужны камере без своей стрелы — например, выездной, когда стрела одна на въезд и выезд
-- (сервер открывает её через реле камеры въезда). Логин/пароль зашифрованы ключом клиента, как у стрел, в API не отдаются.
ALTER TABLE camera ADD COLUMN video_endpoint text;
ALTER TABLE camera ADD COLUMN video_credentials_encrypted bytea;
ALTER TABLE camera ADD CONSTRAINT camera_video_pair CHECK ((video_endpoint IS NULL) = (video_credentials_encrypted IS NULL));
COMMENT ON COLUMN camera.video_endpoint IS 'Адрес камеры для видеопотока: http://IP (как у стрелы HIKVISION_ISAPI); RTSP — порт 554.';
COMMENT ON COLUMN camera.video_credentials_encrypted IS 'Логин/пароль видеопотока, AES-256-GCM DEK клиента; AAD = tenant + версия + camera-video:<id>.';
