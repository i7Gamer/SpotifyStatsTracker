# Stale bytecode caches must not leak into the image
Get-ChildItem -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

docker build --no-cache -t spotify-tracker:nightly .
# cached variant: docker build -t spotify-tracker:nightly .

# PowerShell does not stop on a failed native command, and a failed build leaves
# the PREVIOUS spotify-tracker:nightly in place - so falling through to the tag
# and push below would republish yesterday's image and report a clean run. The
# only visible symptom would be that nothing changed after "a successful push".
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed (exit $LASTEXITCODE). Aborting before tag/push so a stale image cannot be republished."
    exit 1
}

docker tag spotify-tracker:nightly i7gamer/spotify-tracker:nightly

Write-Host "Testing if container starts successfully..."
# Run detached (-d) so the script can continue
docker run -d --name test-tracker spotify-tracker:nightly

# Wait 5 seconds to give the Flask app time to crash if there is a fatal error
Start-Sleep -Seconds 5

# Check if the container is still running
$isRunning = docker inspect -f '{{.State.Running}}' test-tracker 2>$null

# Logs and cleanup happen either way - they are the diagnostic for a failed
# startup, so they must run before the abort exit below rather than after it.
$pushed = $false
if ($isRunning -eq 'true') {
    Write-Host "Container is stable. Stopping test and proceeding with push..."
    docker stop test-tracker | Out-Null
    docker push i7gamer/spotify-tracker:nightly
    $pushed = ($LASTEXITCODE -eq 0)
    if (-not $pushed) {
        Write-Host "Push failed (exit $LASTEXITCODE)."
    }
} else {
    Write-Host "Container crashed on startup. Aborting push. Check logs for details."
}
docker logs test-tracker
docker rm test-tracker

# Exit non-zero whenever nothing was published. Without this the script ends on
# `docker rm`, so "the image never got published" and "everything worked" both
# came back as exit 0.
if (-not $pushed) {
    exit 1
}
