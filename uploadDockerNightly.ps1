Get-ChildItem -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
docker build --no-cache -t spotify-tracker:nightly .
# docker build -t spotify-tracker:nightly .
docker tag spotify-tracker:nightly i7gamer/spotify-tracker:nightly

Write-Host "Testing if container starts successfully..."
# Run detached (-d) so the script can continue
docker run -d --name test-tracker spotify-tracker:nightly

# Wait 5 seconds to give the Flask app time to crash if there is a fatal error
Start-Sleep -Seconds 5

# Check if the container is still running
$isRunning = docker inspect -f '{{.State.Running}}' test-tracker 2>$null

if ($isRunning -eq 'true') {
    Write-Host "Container is stable. Stopping test and proceeding with push..."
    docker stop test-tracker | Out-Null
    docker push i7gamer/spotify-tracker:nightly
} else {
    Write-Host "Container crashed on startup. Aborting push. Check logs for details."
}
docker logs test-tracker
docker rm test-tracker