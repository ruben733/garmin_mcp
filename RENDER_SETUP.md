# Garmin MCP: Upstash y Render

## Estado de la revisiÃ³n

CÃ³digo revisado localmente el 20 de septiembre de 2026. La configuraciÃ³n
remota indicada por el usuario ya contiene las dos variables de Upstash.
No se han publicado cambios ni iniciado un despliegue durante esta revisiÃ³n.
El servicio de Render usa el plan Free, que no permite Shell ni trabajos
puntuales. Las credenciales Upstash no estÃ¡n disponibles en el entorno local.
La prueba real de sincronizaciÃ³n/restauraciÃ³n sigue pendiente.

## ConfiguraciÃ³n de Render

- Usar este Dockerfile y no sustituir su entrypoint. Revisar cualquier Docker
  Command personalizado antes del despliegue; normalmente dejarlo vacÃ­o.
- UPSTASH_REDIS_REST_URL y UPSTASH_REDIS_REST_TOKEN son secretos de ejecuciÃ³n,
  nunca argumentos de construcciÃ³n ni valores versionados. HTTPS obligatorio.
- El token necesita GET, SET y EVAL para la comparaciÃ³n y escritura atÃ³micas.
- UPSTASH_REDIS_KEY es opcional; por defecto garmin:mcp:tokens. Separar cuentas
  y entornos mediante claves distintas.
- GARMINTOKENS=/root/.garminconnect.
- GARMIN_MCP_TRANSPORT=streamable-http y GARMIN_MCP_HOST=0.0.0.0.
  entrypoint.sh utiliza PORT de Render salvo GARMIN_MCP_PORT explÃ­cito.
- Health check: /healthz. No verifica autenticaciÃ³n Garmin ni persistencia.
- El servidor no implementa autenticaciÃ³n HTTP de clientes: proteger /mcp
  mediante una capa de autenticaciÃ³n antes de habilitar acceso a datos reales.
- Usar una instancia escritora. La comparaciÃ³n atÃ³mica evita sobrescribir
  una versiÃ³n remota distinta durante solapamientos, pero no coordina las
  solicitudes simultÃ¡neas de renovaciÃ³n contra Garmin.

## ProtecciÃ³n contra archivos antiguos

Con Upstash configurado, solo se permite arrancar si existe una clave remota
con la estructura completa de tokens que usa garminconnect 0.3.2.
Una clave vacÃ­a, ilegible o invÃ¡lida detiene el arranque. No se importa el
Secret File de Render ni un archivo local en esos casos.
El Secret File garmin_tokens.json puede seguir obsoleto: no debe emplearse
para inicializar Upstash. Tampoco hay que eliminar la clave para forzar una
nueva carga ni volver al entrypoint anterior como soluciÃ³n.

El supervisor conserva la versiÃ³n restaurada antes de lanzar el servidor.
Solo sincroniza cambios completos del archivo, cada cinco segundos y al
cerrar, mediante EVAL y comparaciÃ³n con la versiÃ³n anterior. Si otro proceso
ha cambiado la clave, detiene este escritor para restaurar al reiniciar.
Un reintento tras una respuesta perdida es idempotente. Una caÃ­da de red
conserva el archivo local y permite reintentar. Una interrupciÃ³n abrupta puede
perder el Ãºltimo cambio pendiente.

Sin ambas variables de Upstash se mantiene el modo local y el uso opcional de
GARMIN_TOKEN_SEED (por defecto /etc/secrets/garmin_tokens.json). Una sola
variable presente es un error de configuraciÃ³n.

## Carga inicial segura, antes de desplegar

1. Disponer de las variables Upstash en una sesiÃ³n local privada, sin pegarlas
   en el chat, argumentos de proceso, historial ni archivos versionados.
2. Usar exclusivamente la copia actual verificada de Downloads, nunca el
   Secret File antiguo. La siguiente operaciÃ³n usa SET NX: si ya existe la
   clave, rechaza la carga y no sustituye nada.

```powershell
python src/garmin_mcp/token_persistence.py --initialize-upstash "$env:USERPROFILE\Downloads\garmin_tokens_nuevo.json"
```

3. Si la clave existe, validar en privado su sesiÃ³n Garmin y su procedencia;
   no hacer un SET incondicional ni borrar la clave. No se puede deducir
   que el contenido remoto sea actual simplemente porque exista.
4. Comprobar restauraciÃ³n en un directorio temporal y autenticaciÃ³n Garmin
   sin imprimir archivos, respuestas, excepciones de librerÃ­as ni valores.
5. Con la protecciÃ³n de acceso HTTP verificada, publicar el cÃ³digo e iniciar
   el despliegue. Confirmar /healthz y una operaciÃ³n Garmin de solo lectura.
6. Reiniciar y repetir la operaciÃ³n para verificar persistencia entre arranques.
   /healthz por sÃ­ solo no demuestra que la sesiÃ³n funcione.

La carga inicial puede realizarse sin consola Render desde la sesiÃ³n local
con acceso privado a Upstash; no requiere cambiar a un plan de pago.

## Seguridad y validaciÃ³n local

```powershell
python tests/unit/test_token_persistence.py
docker build -t garmin-mcp-upstash-test .
docker run --rm --network none garmin-mcp-upstash-test python tests/unit/test_token_persistence.py
```

Pruebas con datos sintÃ©ticos. Las peticiones emplean POST, no incluyen datos
en la URL, rechazan redirecciones y no imprimen detalles de errores.
La restauraciÃ³n es atÃ³mica con permisos 0700/0600 en Linux. Upstash contiene
el JSON protegido en trÃ¡nsito por HTTPS; no hay cifrado adicional de aplicaciÃ³n.
Los ignore excluyen exportaciones garmin_tokens*.json, tokens OAuth antiguos,
archivos .env, copias .backup y archivos temporales de tokens. Revisar siempre
los archivos que se aÃ±aden a Git y no activar logs de depuraciÃ³n HTTP/Garmin.

## Resultados locales de esta revisión

- 18/18 pruebas de persistencia en Windows y dentro de la imagen Linux.
- Construcción correcta de garmin-mcp-upstash-test.
- Contenedor sin red externa: /healthz responde 200 usando PORT=18181;
  el supervisor propaga la terminación y finaliza dentro del plazo.
- Imagen comprobada sin archivos garmin_tokens*.json ni .env en /app.
- Los ocho archivos pendientes no contienen valores de la copia actual
  examinada en memoria; no se imprimieron esos valores.
- Ningún archivo de tokens con los nombres conocidos está versionado.
- git diff --check sin errores. Estas comprobaciones no sustituyen una
  prueba real de autenticación Garmin y restauración desde Upstash.

## Referencias

- https://upstash.com/docs/redis/features/restapi
- https://upstash.com/docs/redis/sdks/ts/commands/scripts/eval
- https://render.com/docs/docker-secrets
- https://render.com/docs/deploys
