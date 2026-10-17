#!/bin/sh
# uso: sh /inicio.sh etiqueta   (por ejemplo: sh /inicio.sh web-1)
command -v httpd >/dev/null 2>&1 || apk add --no-cache busybox-extras > /tmp/apk.log 2>&1
echo "$1" > /nombre
mkdir -p /www/cgi-bin
echo "Atendido por: $1" > /www/index.html
httpd -f -p 8080 -h /www
