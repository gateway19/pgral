# pgral
pgral (Photo gallery grid app local)  


# run 
Download lastest [release](https://github.com/gateway19/pgral/releases/)  \
save to folder \
Create shortcut  PROGRAM_PATH/pgral.exe -u "http://127.0.0.1:8095/?path=C:\Users\username\Downloads\&regex=.*\.(png|jpg|jpeg)$" \
or to PROGRAM_PATH/updater.exe -u "http://127.0.0.1:8095/?path=C:\Users\username\Downloads\&regex=.*\.(png|jpg|jpeg)$" -v main for auto update \


# dev 
```cmd
git clone https://github.com/gateway19/pgral/ --branch dev  
cd pgral 
pip install -r requirements.txt 
python main.py 

pyinstaller --onefile --add-data "templates;templates" --name pgral --hidden-import=uvicorn.protocols.http.h11_impl --hidden-import=uvicorn.protocols.websockets.websockets_impl main.py 
go build -o dist/updater.exe main.go
```
