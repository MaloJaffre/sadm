CREATE TABLE user_location(id SERIAL, hfs INTEGER, username VARCHAR);
CREATE USER hfs WITH PASSWORD 'DEFAULT_PASSWORD';
GRANT ALL PRIVILEGES ON DATABASE "hfs" to hfs;
