FROM python:3.11-slim

# Instala o FFmpeg
RUN apt update && apt install -y ffmpeg && apt clean

# Pasta da aplicação
WORKDIR /

# Copia arquivos
COPY . .

# Instala dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Comando para rodar o bot
CMD ["python", "bot.py"]