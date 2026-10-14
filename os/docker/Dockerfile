FROM debian:bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
      bc bison build-essential bzip2 ca-certificates cpio dosfstools e2fsprogs fdisk file flex \
      git gzip libncurses-dev mtools patch perl python3 rsync squashfs-tools unzip util-linux \
      wget xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 builder
ENV LANG=C.UTF-8
WORKDIR /br
