"""Ayudante del laboratorio AndesTickets.

Habla con Floci en http://localhost:4566 (no con AWS) para las operaciones
que la consola web no permite hacer: crear el balanceador con su listener,
registrar instancias y manejar el grupo de Auto Scaling. Tambien usa Docker
para preparar las instancias (que en Floci son contenedores).
Solo usa la libreria estandar de Python."""
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FLOCI = "http://localhost:4566/"
REGION = "us-east-1"

# Programa en JavaScript que arregla la consola (se ejecuta dentro del contenedor de la consola).
PARCHE_JS = r'''const fs = require("fs");
const archivo = "/app/dist/backend/routes/aws/elb.js";
let s = fs.readFileSync(archivo, "utf8");

if (s.includes("subredesPorDefecto")) {
  console.log("Ya estaba parchado. No hago nada.");
  process.exit(0);
}

function cambiar(antes, despues) {
  if (s.split(antes).length !== 2) {
    console.error("No pude aplicar el parche: no encuentro este texto en " + archivo + ":\n" + antes);
    process.exit(1);
  }
  s = s.replace(antes, () => despues);
}

cambiar('const aws_1 = require("../../clients/aws");',
`const aws_1 = require("../../clients/aws");
const client_ec2_parche = require("@aws-sdk/client-ec2");
async function subredesPorDefecto() {
    const r = await (0, aws_1.create)(client_ec2_parche.EC2Client).send(new client_ec2_parche.DescribeSubnetsCommand({}));
    const porZona = new Map();
    for (const x of r.Subnets ?? []) {
        if (x.AvailabilityZone && x.SubnetId && !porZona.has(x.AvailabilityZone)) porZona.set(x.AvailabilityZone, x.SubnetId);
    }
    return [...porZona.values()].slice(0, 2);
}
async function vpcPorDefecto() {
    const r = await (0, aws_1.create)(client_ec2_parche.EC2Client).send(new client_ec2_parche.DescribeVpcsCommand({}));
    const v = (r.Vpcs ?? []).find((x) => x.IsDefault) ?? (r.Vpcs ?? [])[0];
    if (!v || !v.VpcId) throw new Error("No hay VPC por defecto en Floci");
    return v.VpcId;
}`);
cambiar(`if (!body.name || !body.subnets?.length) {
        return c.json({ error: "name and subnets are required" }, 400);
    }`,
`if (!body.name) {
        return c.json({ error: "name is required" }, 400);
    }
    const usaFalsa = !body.subnets?.length || body.subnets.includes("subnet-12345678");
    const subredes = usaFalsa ? await subredesPorDefecto() : body.subnets;`);
cambiar("Subnets: body.subnets,", "Subnets: subredes,");
cambiar("VpcId: body.vpcId,", 'VpcId: body.vpcId === "vpc-default" ? await vpcPorDefecto() : body.vpcId,');

fs.writeFileSync(archivo, s);
console.log("Parche aplicado en " + archivo);
'''


def llamar(servicio, version, accion, **params):
    """Envia una peticion al API de Floci y devuelve el XML sin espacios de nombres."""
    datos = {"Action": accion, "Version": version}
    datos.update(params)
    cuerpo = urllib.parse.urlencode(datos).encode()
    firma = ("AWS4-HMAC-SHA256 Credential=test/20260101/%s/%s/aws4_request, "
             "SignedHeaders=host, Signature=0" % (REGION, servicio))
    req = urllib.request.Request(FLOCI, data=cuerpo, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": firma})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            texto = r.read().decode()
    except urllib.error.HTTPError as e:
        texto = e.read().decode()
    except urllib.error.URLError:
        sys.exit("No puedo conectar con Floci en %s. Ejecuta 'docker compose ps' y revisa que este arriba." % FLOCI)
    try:
        raiz = ET.fromstring(texto)
    except ET.ParseError:
        sys.exit("Floci respondio algo inesperado en %s: %s" % (accion, texto[:200]))
    for el in raiz.iter():
        el.tag = el.tag.split("}")[-1]
    if raiz.tag in ("ErrorResponse", "Error") or raiz.find(".//Error") is not None:
        codigo = raiz.findtext(".//Code") or "Error"
        mensaje = raiz.findtext(".//Message") or ""
        sys.exit("Floci respondio con error en %s: %s - %s" % (accion, codigo, mensaje))
    return raiz


def ec2(accion, **p):
    return llamar("ec2", "2016-11-15", accion, **p)


def elb(accion, **p):
    return llamar("elasticloadbalancing", "2015-12-01", accion, **p)


def asg(accion, **p):
    return llamar("autoscaling", "2011-01-01", accion, **p)


def red_por_defecto():
    """Devuelve (vpc, [subredes de esa vpc en zonas distintas])."""
    vpcs = ec2("DescribeVpcs")
    vpc = None
    for item in vpcs.iter("item"):
        if item.findtext("isDefault") == "true":
            vpc = item.findtext("vpcId")
    if not vpc:
        sys.exit("No encontre la VPC por defecto en Floci.")
    subredes, zonas = [], set()
    for item in ec2("DescribeSubnets").iter("item"):
        if item.findtext("vpcId") == vpc and item.findtext("availabilityZone") not in zonas:
            zonas.add(item.findtext("availabilityZone"))
            subredes.append(item.findtext("subnetId"))
    if len(subredes) < 2:
        sys.exit("Necesito al menos 2 subredes en zonas distintas y solo encontre %d." % len(subredes))
    return vpc, subredes[:2]


def docker(*args):
    """Ejecuta un comando de Docker y devuelve el resultado (texto de salida y codigo)."""
    try:
        return subprocess.run(["docker"] + list(args), capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("No encuentro el comando docker. Abre Docker Desktop y vuelve a abrir la terminal.")


def contenedores_por_ip():
    """Devuelve {ip: nombre de contenedor} de todos los contenedores en marcha.
    La IP privada de una instancia de Floci es la IP de su contenedor."""
    ids = docker("ps", "-q").stdout.split()
    mapa = {}
    if ids:
        formato = "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
        for linea in docker("inspect", "-f", formato, *ids).stdout.splitlines():
            partes = linea.split()
            for ip in partes[1:]:
                mapa[ip] = partes[0].lstrip("/")
    return mapa


def leer_instancias():
    """Lista de (id, tipo, estado, ip) de todas las instancias que ve Floci."""
    res = []
    for it in ec2("DescribeInstances").iter("item"):
        iid = it.findtext("instanceId")
        if iid:
            estado = it.find("instanceState")
            res.append((iid, it.findtext("instanceType") or "-",
                        estado.findtext("name") if estado is not None else "-",
                        it.findtext("privateIpAddress") or "-"))
    return res


def contenedor_de(iid):
    """Nombre del contenedor Docker de una instancia, o termina con un mensaje claro."""
    for i, _, estado, ip in leer_instancias():
        if i == iid:
            nombre = contenedores_por_ip().get(ip)
            if not nombre:
                sys.exit("La instancia %s (estado %s) no tiene un contenedor en marcha. Revisa que este running." % (iid, estado))
            return nombre
    sys.exit("No encuentro la instancia '%s'. Copia el ID completo (empieza por i-) de 'python andes.py instancias'." % iid)


def cmd_instancias():
    mapa = contenedores_por_ip()
    print("%-22s %-10s %-11s %-16s %s" % ("ID", "TIPO", "ESTADO", "IP PRIVADA", "CONTENEDOR"))
    for iid, tipo, estado, ip in leer_instancias():
        print("%-22s %-10s %-11s %-16s %s" % (iid, tipo, estado, ip, mapa.get(ip, "-")))


def ejecutar(*args):
    """Corre un comando de Docker y, si falla, termina mostrando el motivo."""
    r = docker(*args)
    if r.returncode != 0:
        sys.exit("Docker fallo en 'docker %s': %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def cmd_web(pares):
    """web etiqueta=ID ...: copia inicio.sh a cada instancia y lo ejecuta con su etiqueta."""
    if not pares or any("=" not in p for p in pares):
        sys.exit("Uso: python andes.py web web-1=ID1 web-2=ID2 web-3=ID3   (cada ID empieza por i-)")
    if not os.path.exists("inicio.sh"):
        sys.exit("No encuentro inicio.sh. Abre la terminal dentro de la carpeta andestickets.")
    for par in pares:
        etiqueta, iid = par.split("=", 1)
        c = contenedor_de(iid)
        ejecutar("cp", "inicio.sh", c + ":/inicio.sh")
        ejecutar("exec", "-d", c, "sh", "/inicio.sh", etiqueta)
        print("%s: instancia %s, contenedor %s -> iniciando el servidor web (puede tardar unos segundos)..." % (etiqueta, iid, c))
        respuesta = None
        for _ in range(40):
            time.sleep(1)
            r = docker("exec", c, "wget", "-q", "-O", "-", "http://127.0.0.1:8080")
            if r.returncode == 0:
                respuesta = r.stdout.strip()
                break
        if respuesta:
            print("%s: el servidor responde: %s" % (etiqueta, respuesta))
        else:
            print("%s: ATENCION: el servidor no responde despues de 40 segundos. Mira el motivo con: docker exec %s cat /tmp/apk.log" % (etiqueta, c))


def cmd_publicar(args):
    """publicar ARCHIVO ID ...: instala el archivo como /cgi-bin/venta en cada instancia."""
    if len(args) < 2:
        sys.exit("Uso: python andes.py publicar venta-v1 ID1 ID2 ID3")
    archivo = args[0]
    if not os.path.exists(archivo):
        sys.exit("No encuentro el archivo %s. Abre la terminal dentro de la carpeta andestickets." % archivo)
    for iid in args[1:]:
        c = contenedor_de(iid)
        ejecutar("cp", archivo, c + ":/www/cgi-bin/venta")
        ejecutar("exec", c, "chmod", "+x", "/www/cgi-bin/venta")
        print("%s publicado en %s (contenedor %s)" % (archivo, iid, c))


def cmd_red(ids):
    """red ID ...: comprueba que cada instancia encuentra a Floci por nombre y, si no, la conecta."""
    if not ids:
        sys.exit("Uso: python andes.py red ID1 ID2 ID3")
    nombre = docker("ps", "--filter", "label=com.docker.compose.service=floci",
                    "--format", "{{.Names}}").stdout.split()
    if not nombre:
        sys.exit("No encuentro el contenedor de Floci. Revisa 'docker compose ps'.")
    redes = docker("inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                   nombre[0]).stdout.split()
    for iid in ids:
        c = contenedor_de(iid)
        if docker("exec", c, "ping", "-c", "1", "-W", "2", "floci").returncode == 0:
            print("%s: ya encuentra a floci" % iid)
            continue
        ejecutar("network", "connect", redes[0], c)
        ok = docker("exec", c, "ping", "-c", "1", "-W", "2", "floci").returncode == 0
        print("%s: conectada a la red %s, %s" % (iid, redes[0], "ahora encuentra a floci" if ok else "SIGUE sin encontrar a floci"))


def arn_grupo(nombre="andes-tg"):
    """Busca un grupo de destino por nombre entre todos los que hay; devuelve su ARN o None."""
    for g in elb("DescribeTargetGroups").iter("member"):
        if g.findtext("TargetGroupName") == nombre:
            return g.findtext("TargetGroupArn")
    return None


def grupo_y_listener(lb, vpc, ids):
    """Usa (o crea) andes-tg, registra las instancias y crea el listener HTTP 8080 del balanceador lb."""
    tg = arn_grupo()
    if tg:
        print("Grupo de destino encontrado: andes-tg (lo creaste en la consola)")
    else:
        tg = elb("CreateTargetGroup", Name="andes-tg", Protocol="HTTP", Port="8080",
                 VpcId=vpc, TargetType="instance").findtext(".//TargetGroupArn")
        print("Grupo de destino creado: andes-tg")
    params = {"TargetGroupArn": tg}
    for n, iid in enumerate(ids, 1):
        params["Targets.member.%d.Id" % n] = iid
        params["Targets.member.%d.Port" % n] = "8080"
    elb("RegisterTargets", **params)
    print("Instancias registradas:", ", ".join(ids))
    elb("CreateListener", LoadBalancerArn=lb, Protocol="HTTP", Port="8080",
        **{"DefaultActions.member.1.Type": "forward",
           "DefaultActions.member.1.TargetGroupArn": tg})
    print("Listener creado: HTTP 8080 -> andes-tg")
    print("Listo. Abre http://localhost:8080 (los targets pasan a healthy en unos segundos).")


def cmd_arreglar_consola():
    """Aplica PARCHE_JS dentro del contenedor de la consola y lo reinicia."""
    consola = [n for n in docker("ps", "--format", "{{.Names}}").stdout.split() if "dash" in n.lower()]
    if not consola:
        sys.exit("No encuentro el contenedor de la consola. Abre http://localhost:4566/_floci/ui, espera a que cargue y repite.")
    c = consola[0]
    with open("parche-consola.js", "w", encoding="utf-8") as f:
        f.write(PARCHE_JS)
    try:
        ejecutar("cp", "parche-consola.js", c + ":/tmp/parche-consola.js")
    finally:
        os.remove("parche-consola.js")
    print(ejecutar("exec", c, "node", "/tmp/parche-consola.js").stdout.strip())
    ejecutar("restart", c)
    print("Consola reiniciada (contenedor %s). Espera 10 segundos y recarga la pagina." % c)


def cmd_balanceador(ids):
    if not ids:
        sys.exit("Uso: python andes.py balanceador ID1 ID2 ID3   (IDs de tus tres instancias web)")
    vpc, subredes = red_por_defecto()
    lb = elb("CreateLoadBalancer", Name="andes-alb", Type="application", Scheme="internet-facing",
             **{"Subnets.member.1": subredes[0], "Subnets.member.2": subredes[1]}
             ).findtext(".//LoadBalancerArn")
    print("Balanceador creado: andes-alb")
    grupo_y_listener(lb, vpc, ids)


def cmd_completar(args):
    """completar NOMBRE ID ...: completa el balanceador que creaste en la consola (y su grupo andes-tg)."""
    if len(args) < 2:
        sys.exit("Uso: python andes.py completar NOMBRE-DEL-BALANCEADOR ID1 ID2 ID3   (por ejemplo: completar elb-1 i-... i-... i-...)")
    lb = elb("DescribeLoadBalancers", **{"Names.member.1": args[0]}).findtext(".//LoadBalancerArn")
    if not lb:
        sys.exit("No encuentro un balanceador llamado '%s'. Revisa el nombre en la pestaña Load Balancers." % args[0])
    vpc, _ = red_por_defecto()
    print("Balanceador encontrado: %s" % args[0])
    grupo_y_listener(lb, vpc, args[1:])


def cmd_registrar(ids):
    if not ids:
        sys.exit("Uso: python andes.py registrar ID   (ID de la instancia que quieres volver a registrar)")
    arn = arn_grupo()
    if not arn:
        sys.exit("No encuentro el grupo de destino andes-tg. Crealo en la consola (ELB, Target Groups).")
    params = {"TargetGroupArn": arn}
    for n, iid in enumerate(ids, 1):
        params["Targets.member.%d.Id" % n] = iid
        params["Targets.member.%d.Port" % n] = "8080"
    elb("RegisterTargets", **params)
    print("Registradas en andes-tg:", ", ".join(ids))


def cmd_redis():
    r = llamar("elasticache", "2015-02-02", "DescribeReplicationGroups")
    hay = False
    print("%-20s %-12s %s" % ("GRUPO", "ESTADO", "PUERTO"))
    for g in r.findall(".//ReplicationGroups/member"):
        gid = g.findtext("ReplicationGroupId")
        if gid:
            hay = True
            print("%-20s %-12s %s" % (gid, g.findtext("Status") or "-", g.findtext(".//PrimaryEndpoint/Port") or "(aun no disponible)"))
    if not hay:
        print("No hay grupos de replicacion. Crea andes-redis en la consola (ElastiCache).")


def cmd_estado():
    arn = arn_grupo()
    if not arn:
        sys.exit("No encuentro el grupo de destino andes-tg. Crealo en la consola (ELB, Target Groups).")
    print("%-22s %-6s %-12s %s" % ("INSTANCIA", "PUERTO", "ESTADO", "MOTIVO"))
    for m in elb("DescribeTargetHealth", TargetGroupArn=arn).iter("member"):
        t = m.find("Target")
        if t is None:
            continue
        h = m.find("TargetHealth")
        print("%-22s %-6s %-12s %s" % (t.findtext("Id"), t.findtext("Port") or "-",
              h.findtext("State"), h.findtext("Reason") or ""))


def cmd_crear_grupo():
    _, subredes = red_por_defecto()
    asg("CreateAutoScalingGroup", AutoScalingGroupName="andes-asg",
        **{"LaunchTemplate.LaunchTemplateName": "andes-plantilla",
           "LaunchTemplate.Version": "$Latest"},
        MinSize="1", MaxSize="4", DesiredCapacity="2", VPCZoneIdentifier=",".join(subredes))
    print("Grupo andes-asg creado (minimo 1, maximo 4, deseado 2). En unos segundos aparecen 2 instancias nuevas.")


def cmd_escalar(n):
    asg("SetDesiredCapacity", AutoScalingGroupName="andes-asg", DesiredCapacity=str(n))
    print("Capacidad deseada de andes-asg: %s. Espera unos segundos y revisa las instancias." % n)


def cmd_grupo():
    for g in asg("DescribeAutoScalingGroups").findall(".//AutoScalingGroups/member"):
        nombre = g.findtext("AutoScalingGroupName")
        if nombre:
            ids = [i.findtext("InstanceId") for i in g.iter("member") if i.findtext("InstanceId")]
            print("%s  min=%s max=%s deseado=%s  instancias=%d %s" % (
                nombre, g.findtext("MinSize"), g.findtext("MaxSize"),
                g.findtext("DesiredCapacity"), len(ids), ids))


AYUDA = """andes.py version 7. Uso:
  python andes.py arreglar-consola           arregla el error 500 al crear balanceadores y grupos en la consola (una sola vez)
  python andes.py instancias                 lista tus instancias (ID, tipo, estado, IP, contenedor)
  python andes.py web web-1=ID1 web-2=ID2    instala inicio.sh en cada instancia con su etiqueta
  python andes.py publicar ARCHIVO ID1 ID2   instala venta-v1 o venta-v2 en cada instancia
  python andes.py red ID1 ID2 ID3            conecta las instancias a la red de Floci si hace falta
  python andes.py balanceador ID1 ID2 ID3    crea andes-tg, registra las instancias y crea andes-alb con su listener
  python andes.py completar NOMBRE ID1 ID2   completa un balanceador creado en la consola (grupo, instancias y listener)
  python andes.py registrar ID               vuelve a registrar una instancia en andes-tg
  python andes.py redis                      muestra el puerto de tu Redis (andes-redis)
  python andes.py estado                     muestra si cada instancia esta healthy
  python andes.py crear-grupo                crea andes-asg (min 1, max 4, deseado 2)
  python andes.py escalar N                  cambia la capacidad deseada de andes-asg
  python andes.py grupo                      muestra el estado de andes-asg"""

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(AYUDA)
    elif a[0] == "arreglar-consola":
        cmd_arreglar_consola()
    elif a[0] == "instancias":
        cmd_instancias()
    elif a[0] == "balanceador":
        cmd_balanceador(a[1:])
    elif a[0] == "web":
        cmd_web(a[1:])
    elif a[0] == "publicar":
        cmd_publicar(a[1:])
    elif a[0] == "red":
        cmd_red(a[1:])
    elif a[0] == "completar":
        cmd_completar(a[1:])
    elif a[0] == "registrar":
        cmd_registrar(a[1:])
    elif a[0] == "redis":
        cmd_redis()
    elif a[0] == "estado":
        cmd_estado()
    elif a[0] == "crear-grupo":
        cmd_crear_grupo()
    elif a[0] == "escalar" and len(a) == 2 and a[1].isdigit():
        cmd_escalar(a[1])
    elif a[0] == "grupo":
        cmd_grupo()
    else:
        print("Comando no reconocido: %s\n" % " ".join(a))
        print(AYUDA)
