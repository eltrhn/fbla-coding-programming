import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';

import { catchError, map, tap } from 'rxjs/operators';
import { Observable } from 'rxjs/Observable';

import { Role, HttpOptions } from './classes';
const httpOptions = HttpOptions;

import { Globals } from './globals';

@Injectable()
export class RoleService {
  private roleInfoURL = '/api/roles/detail';
  private roleEditURL = '/api/roles/edit';
  private roleDelURL = '/api/roles/delete';
  private roleCreateURL = '/api/location/roles/add';
  
  constructor(
    private globals: Globals,
    private http: HttpClient
  ) {}
  
  getArrs(rID): Observable<any> {
    return this.http.get<any>(this.roleInfoURL, {params: {rid: rID}});
  }
  
  modify(rID, name, perms, limits, locks): Observable<any> {
    return this.http.post(this.roleEditURL, {rid: rID, name: name, seqs: {perms: perms, limits: limits, locks: locks}}, httpOptions);
  }
  
  create(name, perms, limits, locks): Observable<any> {
    return this.http.post(this.roleCreateURL, {name: name, seqs: {perms: perms, limits: limits, locks: locks}}, httpOptions);
  }
  
  delete(rID): Observable<any> {
    return this.http.put(this.roleDelURL, {rid: rID});
  }
  
}
