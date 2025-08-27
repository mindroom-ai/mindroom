# Debugging Docker API Issues

## To debug the 404 error:

1. **Check the Network tab in browser DevTools**:
   - Open DevTools (F12)
   - Go to Network tab
   - Click on the "External" tab in the app
   - Look for the failed request
   - Check the full URL it's trying to fetch

2. **Check what API_BASE_URL is set to**:
   - In the browser console, you can check by running:
   ```javascript
   // This will show what the frontend thinks the API URL is
   console.log(import.meta.env.VITE_API_URL);
   ```

3. **Test the API directly**:
   - Try accessing the API endpoint directly in the browser:
   - If using Docker with domain: `https://yourdomain.com/api/matrix/agents/rooms`
   - If local: `http://localhost:8765/api/matrix/agents/rooms`

4. **Check Docker logs**:
   ```bash
   docker logs <container-name> -f
   ```

## Possible issues:

1. **The 404 might be for the page route, not the API**:
   - SPAs need all routes to serve index.html
   - The error `unconfigured-rooms:1` suggests it might be trying to load a page

2. **API_BASE_URL might not be empty**:
   - Check if VITE_API_URL is truly empty in the Docker container
   - Run: `docker exec <container> env | grep VITE`

3. **Traefik routing issue**:
   - The `/api` routes might not be properly routed to the backend port
