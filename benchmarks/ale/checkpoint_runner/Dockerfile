FROM agentslastexam/ale-qemu@sha256:cc3ead471566c27f7c394c8748ff2e69124f310dbe35fce5e4a5177bc947232f
COPY boot.sh /run/boot.sh
RUN chmod 755 /run/boot.sh
ENV CPU_FLAGS="-invtsc,migratable=yes"
