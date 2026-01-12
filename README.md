# jobs
Los jobs de RabbitMQ que recopilan, parsean y limpian las noticias. 

## Despliegue / Producción

El fichero `compose.yml` incluye el servicio `cloudflared` marcado con el perfil `production`. Esto significa que por defecto (en desarrollo) `cloudflared` no se crea ni se inicia.

Para arrancar los servicios en desarrollo (sin `cloudflared`):

```bash
docker compose up --build
```

Para arrancar en producción e incluir `cloudflared`:

```bash
docker compose --profile production up --build -d
```
o
```bash
COMPOSE_PROFILES=production docker compose up --build -d
```

Notas:
- `cloudflared` solo se añadirá al conjunto de servicios cuando el perfil `production` esté activo.

## Equipo

El equipo esta compuesto por:
- Ariadna Mantilla
- Eulalia Peiret
- Ivan Moreno
- Laura Apolzan
