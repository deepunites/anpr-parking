# Круглосуточная запись камер на диск сервера (deploy/truenas/video.sh): ffmpeg копирует RTSP-поток камеры в файлы
# без перекодирования. Пользователь 568 — «apps» TrueNAS: ему video.sh отдаёт папки записи и список потоков.
FROM alpine:3.22
RUN apk add --no-cache ffmpeg tzdata
COPY record.sh /usr/local/bin/record.sh
USER 568:568
ENTRYPOINT ["/bin/sh", "/usr/local/bin/record.sh"]
