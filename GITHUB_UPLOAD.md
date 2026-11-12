# Subir MAGICZIP a GitHub

Esta carpeta es la entrega limpia de MagicZip. No contiene ejecutables `.exe`, entornos virtuales, cachés ni resultados de pruebas.

## Uso local

1. Ejecuta `INICIAR_MAGICZIP.bat`.
2. Si Python no está preparado, crea el entorno:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

3. Vuelve a ejecutar el BAT.

## GitHub

Desde una terminal ubicada dentro de esta carpeta:

```bat
git init
git add .
git commit -m "MagicZip local sin ejecutable"
git branch -M main
git remote add origin https://github.com/USUARIO/REPOSITORIO.git
git push -u origin main
```

Reemplaza la URL del repositorio por la que corresponda. El procesamiento se mantiene local en `127.0.0.1`; no se cargan documentos a servidores externos.
