ARG BUILD_FROM
FROM ${BUILD_FROM}

# ffmpeg: transcode/mux to MPEG-TS HLS. bash: run script (mapfile). python3 + py3-yaml:
# config parsing + HTTP/UI server. procps: pkill, used to reload camera workers.
RUN apk add --no-cache bash ffmpeg python3 py3-yaml procps

COPY run.sh /run.sh
COPY hlsd.py /hlsd.py
COPY ui.py /ui.py
COPY injector.py /injector.py
COPY config.yaml /manifest.yaml
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
