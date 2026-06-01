# Lambda container image with Chrome + nodriver + Xvfb
# Base: AWS Lambda Python 3.12 (Amazon Linux 2023)
#
# nodriver (https://github.com/ultrafunkamsterdam/nodriver) speaks raw CDP,
# so we DON'T need chromedriver. It's the same engine EzSolver uses.

FROM public.ecr.aws/lambda/python:3.12

# ─────────────────────────────────────────────────────────────
# Install Google Chrome (stable), Xvfb and all runtime libs Chrome needs.
# `chromium` isn't in AL2023's default repos, so we use Google's RPM.
# No chromedriver needed — nodriver uses raw CDP.
# ─────────────────────────────────────────────────────────────
RUN dnf install -y \
        xorg-x11-server-Xvfb \
        xorg-x11-utils \
        liberation-fonts \
        nss \
        nspr \
        atk \
        at-spi2-atk \
        cups-libs \
        libdrm \
        libxkbcommon \
        libXcomposite \
        libXdamage \
        libXfixes \
        libXrandr \
        libXtst \
        libXi \
        mesa-libgbm \
        alsa-lib \
        pango \
        gtk3 \
        wget \
        unzip \
        tar \
        gzip \
        gcc \
        make \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# xdotool is not in AL2023 repos — build it from source (small, ~200KB binary)
RUN dnf install -y libX11-devel libXtst-devel libXi-devel libxkbcommon-devel \
        libXinerama-devel perl-podlators \
    && curl -fsSLo /tmp/xdotool.tar.gz \
        https://github.com/jordansissel/xdotool/releases/download/v3.20211022.1/xdotool-3.20211022.1.tar.gz \
    && cd /tmp && tar -xzf xdotool.tar.gz \
    && cd xdotool-3.20211022.1 && make && make install \
    && cd / && rm -rf /tmp/xdotool* \
    && /usr/local/bin/xdotool --version \
    && ln -sf /usr/local/bin/xdotool /usr/bin/xdotool

# Google Chrome stable — download via curl, install with rpm (handles deps via dnf)
RUN curl -fsSLo /tmp/chrome.rpm \
        https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm \
 && ls -lh /tmp/chrome.rpm \
 && rpm -ivh --nodeps /tmp/chrome.rpm \
 && rm -f /tmp/chrome.rpm \
 && /opt/google/chrome/chrome --version || /usr/bin/google-chrome --version

# ─────────────────────────────────────────────────────────────
# Python deps
# ─────────────────────────────────────────────────────────────
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# ─────────────────────────────────────────────────────────────
# Function code
# ─────────────────────────────────────────────────────────────
COPY lambda_handler.py ahrefs.json ${LAMBDA_TASK_ROOT}/

# nodriver writes its profile to /tmp (the only writable dir on Lambda)
ENV CHROME_BINARY=/opt/google/chrome/chrome \
    USE_XVFB=1 \
    HOME=/tmp \
    TS_PROFILE_DIR=/tmp/ts_profile \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_CONFIG_HOME=/tmp/.config \
    PYTHONUNBUFFERED=1

CMD ["lambda_handler.lambda_handler"]
