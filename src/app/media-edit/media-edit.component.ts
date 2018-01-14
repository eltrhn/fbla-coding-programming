import { Component, OnInit, Input } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { Location } from '@angular/common';

import { MediaService } from '../media.service';

import { Globals } from '../globals';

@Component({
  selector: 'app-media-edit',
  templateUrl: './media-edit.component.html',
  styleUrls: ['./media-edit.component.css']
})
export class MediaEditComponent implements OnInit {
  item: any;
  mID: string;
  msg: string;
  
  constructor(
    public globals: Globals,
    public location: Location,
    private route: ActivatedRoute,
    private mediaService: MediaService
  ) {}
  
  ngOnInit(): void {
    this.mID = this.route.snapshot.paramMap.get('mID');
    if (this.mID == 'new') {
      this.makeItem();
    } else {
      this.getItem();
    }
  }
  
  getItem(): void {
    this.mediaService.getInfo(this.mID)
      .subscribe(item => this.item = item.info);
  }
  
  makeItem(): void {
    this.item = {
      mid: null,
      title: null,
      author: null,
      published: null,
      image: null,
      type: null,
      genre: null,
      isbn: null,
      price: null,
      length: null,
      available: null,
    }
  }
  
  checkAllNecessary() {
    let item = this.item;
    // uggo
    return item.title && item.author && item.type && item.isbn && item.price && item.length;
  }
  
  submit(): void {
    if (this.mID == 'new') {
      this.mediaService.createItem(this.item)
        .subscribe(
          resp => {
            this.item.image = resp.image;
            this.mID = resp.mid;
            this.item.mid = resp.mid;
            this.msg = 'Successfully created.'
          },
          err => {
            this.msg = err.error?err.error:'Error.'
          }
        );
    } else {
      this.mediaService.editItem(this.item)
        .subscribe(resp => this.msg = 'Successfully edited.', err => this.msg = err.error?err.error:'Error.')
    }
  }
}
