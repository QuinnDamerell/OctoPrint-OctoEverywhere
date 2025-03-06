# Start with the latest alpine, for a solid base,
# since we need some advance binaries for things like pillow and ffmpeg.
FROM alpine:3.21.3

# Create a non-root user to run, so we don't run as root.
# There's no need to run as root and it helps some platforms like openshift.
RUN adduser -Ss /bin/bash app -h /app -g root -u 1001

WORKDIR /app

# Define some user vars we will use for the image.
# These are read in the docker_octoeverywhere module, so they must not change!
ENV USER=app
ENV REPO_DIR=/app/octoeverywhere
ENV VENV_DIR=/app/octoeverywhere-env
# This is a special dir that the user MUST mount to the host, so that the data is persisted.
# If this is not mounted, the printer will need to be re-linked every time the container is remade.
ENV DATA_DIR=/data/

# Install the required packages.
# Any packages here should be mirrored in the install script - and any optional pillow packages done inline.
# GCC, python3-dev, and musl-dev are required for pillow, and jpeg-dev and zlib-dev are required for jpeg support.
RUN apk add --no-cache curl ffmpeg jq python3 python3-dev gcc musl-dev py3-pip py3-virtualenv jpeg-dev libjpeg-turbo-dev zlib-dev py3-pillow libffi-dev

# Timezone setup steps
# These steps are necessary to add timezone support to the container and allow for setting the timezone
# This allows the log files to show the correct local time
RUN apk add --no-cache tzdata
ENV TZ=Etc/GMT
RUN cp /usr/share/zoneinfo/Etc/GMT /etc/localtime

#
# We decided to not run the installer, since the point of the installer is to setup the env, build the launch args, and setup the service.
# Instead, we will manually run the smaller subset of commands that are required to get the env setup in docker.
# Note that if this ever becomes too much of a hassle, we might want to revert back to using the installer, and supporting a headless install.
#
RUN virtualenv -p /usr/bin/python3 ${VENV_DIR}
RUN ${VENV_DIR}/bin/python -m pip install --upgrade pip

# Copy the entire repo into the image, do this as late as possible to avoid rebuilding the image every time the repo changes.
COPY ./ ${REPO_DIR}/
RUN ${VENV_DIR}/bin/pip3 install --require-virtualenv --no-cache-dir -q -r ${REPO_DIR}/requirements.txt

# Install the optional packages for zstandard compression.
# THIS VERSION STRING MUST STAY IN SYNC with Compression.ZStandardPipPackageString
RUN apk add zstd
RUN ${VENV_DIR}/bin/pip3 install --require-virtualenv --no-cache-dir -q "zstandard>=0.21.0,<0.23.0"

# Ensure directories have correct ownership. Having the group set to root(0) and writable by group will allow this to run on openshift
RUN chown -R 1001:0 /app && chmod -R g+wx /app

# For docker, we use our docker_octoeverywhere host to handle the runtime setup and launch of the service.
WORKDIR ${REPO_DIR}

# Use the full path to the venv, we must use this [] notation for our ctl-c handler to work in the container.
ENTRYPOINT ["/app/octoeverywhere-env/bin/python", "-m", "docker_octoeverywhere"]
