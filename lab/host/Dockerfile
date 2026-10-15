# The six servers: a small Linux with the real ip and ping tools
# (ping -M do tests that full 1500 byte packets cross the fabric unfragmented).
FROM alpine:3.22
RUN apk add --no-cache iproute2 iputils tcpdump
CMD ["sleep", "infinity"]
