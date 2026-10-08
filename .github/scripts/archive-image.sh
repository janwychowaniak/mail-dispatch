#!/usr/bin/env bash
# Save a published image into the archive attached to its release, and prove that the archive
# loads, into a store that no longer holds the image, as the same image (SPEC §12).
#
#   archive-image.sh <repository> <version> <image id>
#
# The image must be present as <repository>:<version>. It is saved by that tag, never by its Id:
# an archive saved by Id loads with no tag at all, and compose cannot see an untagged image.
# The Id is what a receiving host compares after `docker load`: it is the digest of the image's
# configuration, which the archive carries byte for byte, while the registry's digest does not
# survive the transfer.
set -euo pipefail

repository=$1
version=$2
expected=$3
image="$repository:$version"
archive="mail-dispatch-$version.tar.gz"

actual=$(docker image inspect --format '{{.Id}}' "$image")
if [[ "$actual" != "$expected" ]]; then
    echo "::error::$image is $actual, but the image that was built is $expected"
    exit 1
fi

docker save "$image" | gzip -n > "$archive"
sha256sum "$archive" > "$archive.sha256"

# The round trip. A load over an image that is still present changes nothing and reads as
# success, so the removal is checked rather than assumed.
docker rmi --force "$expected" > /dev/null
if docker image inspect "$expected" > /dev/null 2>&1; then
    echo "::error::$expected is still present after its removal, so a load would prove nothing"
    exit 1
fi
docker load --input "$archive"
loaded=$(docker image inspect --format '{{.Id}}' "$image")
if [[ "$loaded" != "$expected" ]]; then
    echo "::error::$archive loads as $loaded, not as $expected"
    exit 1
fi
sha256sum -c "$archive.sha256"
echo "$archive holds $image, Id $loaded"
